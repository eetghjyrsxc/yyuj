/**
 * 手机桥 · B 档跑腿脚本（在 AutoJs6 里跑）
 * ============================================================
 * 干什么：长轮询沙箱那侧的桥（127.0.0.1:8899）取「界面指令」，执行完把结果回执。
 *         沙箱的入口是 `运维工具/手机桥/手机控制.sh 读屏 / 点 / 输入 / 按键 / 启动 / 等 / 提示`。
 *
 * 怎么用：
 *   1. 手机上先跑桥：Termux 里 `bash ~/bridge.sh`
 *   2. AutoJs6 里打开本文件 → 点运行（第一次会要无障碍权限，给上）
 *   3. 跑起来会弹一条 toast「跑腿脚本上线」；之后它会一直挂着等活
 *      （要停：在 AutoJs6 里停止脚本，或让沙箱发一条 `界面停`）
 *
 * 安全边界（都在本文件里，沙箱那侧改不了）：
 *   · 只读指令（toast / ping / screen）随时可用 —— 它们不点、不碰、只读文字
 *   · 动作指令（tap / input / key）**只在前台 App 属于 ALLOW_PKGS 时才执行**
 *   · 启动指令（app）**只允许启动 ALLOW_PKGS 里的包**
 *   · ALLOW_PKGS 默认为空 ⇒ 任何动作都会被拒（要先在下面点名放行）
 *   · 不做任何绕过检测/伪装的事；看不清、点不到就如实报失败
 */

// ───────────────────────── 配置 ─────────────────────────
var BASE = "http://127.0.0.1:8899";      // 桥（沙箱的 127.0.0.1 就是手机的 127.0.0.1）
var WAIT_S = 20;                          // 长轮询挂多久（秒）
var ALLOW_PKGS = [                        // ★ 动作白名单：只放行你点名的 App（留空=什么都不点）
    // "com.android.settings",            // 系统设置
    // "com.huawei.hidisk",               // 文件管理（机型不同包名可能不一样）
    // "com.android.gallery3d",           // 相册
];
var ALLOW_ANY = false;                    // 临时放开（调试用）：true = 不检查白名单，慎用
var SHOW_CONSOLE = false;                 // 要不要弹个悬浮日志窗（悬浮窗本身是风控信号之一，默认不开；
                                          //   想看日志就在 AutoJs6 菜单里翻，或临时改成 true）
var MAX_ROWS = 60;                        // 读屏最多回传多少行

// ───────────────────────── 小工具 ─────────────────────────
function httpReq(method, path, body) {
    var conn = new java.net.URL(BASE + path).openConnection();
    conn.setConnectTimeout(3000);
    conn.setReadTimeout((WAIT_S + 15) * 1000);
    conn.setRequestMethod(method);
    if (body != null) {
        conn.setDoOutput(true);
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        var os = conn.getOutputStream();
        os.write(new java.lang.String(body).getBytes("UTF-8"));
        os.flush();
        os.close();
    }
    var code = conn.getResponseCode();
    var st = (code >= 400) ? conn.getErrorStream() : conn.getInputStream();
    var txt = "";
    if (st != null) {
        var br = new java.io.BufferedReader(new java.io.InputStreamReader(st, "UTF-8"));
        var sb = new java.lang.StringBuilder(), line;
        while ((line = br.readLine()) != null) sb.append(line);
        br.close();
        txt = sb.toString();
    }
    conn.disconnect();
    return { code: code, body: txt };
}

function jparse(txt) {
    try { return new org.json.JSONObject(txt); } catch (e) { return null; }
}

function shortClass(cn) {
    if (!cn) return "";
    var parts = ("" + cn).split(".");
    return parts[parts.length - 1];
}

/** 这个节点自己可点吗？不可点就往上找可点的父节点（最多 6 层）。 */
function clickableTarget(node) {
    var n = node;
    for (var i = 0; i < 6 && n != null; i++) {
        if (n.clickable && n.clickable()) return n;
        n = n.parent ? n.parent() : null;
    }
    return null;
}

function centerOf(node) {
    var b = node.bounds();
    if (b == null || b.width() <= 0 || b.height() <= 0) return null;
    return { x: Math.round(b.centerX()), y: Math.round(b.centerY()) };
}

function inWhitelist(pkg) {
    if (ALLOW_ANY) return true;
    for (var i = 0; i < ALLOW_PKGS.length; i++) if (ALLOW_PKGS[i] === pkg) return true;
    return false;
}

function nowPkg() {
    try { return currentPackage() || ""; } catch (e) { return ""; }
}

function nowAct() {
    try { return currentActivity() || ""; } catch (e) { return ""; }
}

// ───────────────────────── 指令实现 ─────────────────────────

/** 读屏：把当前界面的文字控件压成一份摘要（不截图，纯控件树） */
function doScreen(cmd) {
    var max = cmd.optInt("lines", MAX_ROWS);
    var rows = [], seen = {};
    function add(text, cls, node) {
        if (rows.length >= max) return;
        var c = centerOf(node);
        if (c == null) return;
        var k = text + "@" + c.x + "," + c.y;
        if (seen[k]) return;
        seen[k] = 1;
        var tgt = clickableTarget(node);
        rows.push({
            t: ("" + text).substring(0, 80),          // 文本（截断，别把长文整段回传）
            c: shortClass(cls),                        // 控件类型
            k: tgt != null ? 1 : 0,                    // 可点吗
            x: c.x, y: c.y
        });
    }
    var nodes = textMatches(/.+/).find();
    for (var i = 0; i < nodes.length && rows.length < max; i++) {
        var n = nodes[i];
        add(n.text(), n.className(), n);
    }
    var descs = descMatches(/.+/).find();
    for (var j = 0; j < descs.length && rows.length < max; j++) {
        var d = descs[j];
        if (d.text() != null && d.text() !== "") continue;   // 有文字的上面已经收过了
        add("[" + d.desc() + "]", d.className(), d);
    }
    return { pkg: nowPkg(), act: nowAct(), w: device.width, h: device.height,
             rows: rows, truncated: rows.length >= max };
}

function doTap(cmd) {
    var pkg = nowPkg();
    if (!inWhitelist(pkg))
        return { ok: false, error: "当前前台是 " + pkg + "，不在 ALLOW_PKGS 白名单里，拒绝点击" };

    if (cmd.has("x") && cmd.has("y")) {
        var x = cmd.optInt("x"), y = cmd.optInt("y");
        var ok = click(x, y);
        return ok ? { ok: true, how: "坐标", x: x, y: y, pkg: pkg }
                  : { ok: false, error: "坐标点击没成功（" + x + "," + y + "）" };
    }
    var want = cmd.optString("text", "");
    if (!want) return { ok: false, error: "既没给 text 也没给 x/y" };
    var n = text(want).findOne(3000) || textContains(want).findOne(2000);
    if (n == null) return { ok: false, error: "屏幕上没找到「" + want + "」" };
    var tgt = clickableTarget(n);
    var hit = false;
    if (tgt != null && tgt.click && tgt.click()) hit = true;
    if (!hit) {                                  // 控件点击不成，退回坐标点击
        var c = centerOf(n);
        if (c != null) hit = click(c.x, c.y);
    }
    return hit ? { ok: true, how: "控件", text: want, pkg: pkg }
               : { ok: false, error: "找到「" + want + "」但点不动（可能被遮挡/不可点）" };
}

function doInput(cmd) {
    var pkg = nowPkg();
    if (!inWhitelist(pkg))
        return { ok: false, error: "当前前台是 " + pkg + "，不在 ALLOW_PKGS 白名单里，拒绝输入" };
    var txt = cmd.optString("text", "");
    if (!txt) return { ok: false, error: "没给 text" };
    try {
        setText(txt);
        sleep(300);
        return { ok: true, how: "setText", len: txt.length, pkg: pkg };
    } catch (e) {}
    var e2 = className("android.widget.EditText").findOnce();
    if (e2 != null && e2.setText(txt)) {
        sleep(300);
        return { ok: true, how: "节点 setText", len: txt.length, pkg: pkg };
    }
    return { ok: false, error: "没找到可输入的输入框（先点一下输入框再试）" };
}

function doKey(cmd) {
    var pkg = nowPkg();
    if (!inWhitelist(pkg))
        return { ok: false, error: "当前前台是 " + pkg + "，不在 ALLOW_PKGS 白名单里，拒绝按键" };
    var k = ("" + cmd.optString("key", "back")).toLowerCase();
    if (k === "back" || k === "返回") back();
    else if (k === "home" || k === "桌面") home();
    else if (k === "recents" || k === "最近") recents();
    else return { ok: false, error: "不认识的按键：" + k + "（back/home/recents）" };
    sleep(500);
    return { ok: true, key: k, pkg: pkg, now: nowPkg() };
}

function doApp(cmd) {
    var target = cmd.optString("pkg", "");
    if (!target) return { ok: false, error: "没给 pkg" };
    if (!inWhitelist(target))
        return { ok: false, error: target + " 不在 ALLOW_PKGS 白名单里，拒绝启动" };
    var ok = target.indexOf(".") > 0 ? app.launchPackage(target) : app.launchApp(target);
    sleep(800);
    return ok ? { ok: true, pkg: target, now: nowPkg() }
              : { ok: false, error: "启动失败：" + target + "（包名对不对？）" };
}

function doWait(cmd) {
    var want = cmd.optString("text", "");
    var ms = cmd.optInt("ms", 10000);
    if (!want) return { ok: false, error: "没给 text" };
    var n = textContains(want).findOne(ms);
    return n != null ? { ok: true, text: want, ms: ms, pkg: nowPkg() }
                     : { ok: false, error: "等了 " + ms + "ms 也没等到「" + want + "」" };
}

function runCmd(cmd) {
    var kind = cmd.optString("kind", "");
    if (kind === "ping")
        return { ok: true, pkg: nowPkg(), act: nowAct(), w: device.width, h: device.height,
                 allow: ALLOW_PKGS, allow_any: ALLOW_ANY, ts: Date.now() };
    if (kind === "toast") { toast(cmd.optString("text", "（空）")); return { ok: true }; }
    if (kind === "screen") return doScreen(cmd);
    if (kind === "tap") return doTap(cmd);
    if (kind === "input") return doInput(cmd);
    if (kind === "key") return doKey(cmd);
    if (kind === "app") return doApp(cmd);
    if (kind === "wait") return doWait(cmd);
    return { ok: false, error: "不认识的指令：" + kind };
}

// ───────────────────────── 主循环 ─────────────────────────
auto.waitFor();                 // 没开无障碍就在这里停下并跳到设置页
toast("跑腿脚本上线：开始等活");
if (SHOW_CONSOLE) console.show();

var STOP = false, idle = 0;
while (!STOP) {
    var got = null;
    try {
        got = httpReq("GET", "/api/ui/next?wait=" + WAIT_S
            + "&pkg=" + encodeURIComponent(nowPkg())
            + "&act=" + encodeURIComponent(nowAct()));
    } catch (e) {
        console.warn("连不上桥（" + e + "），2 秒后重试 —— 手机上 Termux 里的桥还开着吗？");
        sleep(2000);
        continue;
    }
    if (got.code !== 200) { sleep(1500); continue; }

    var obj = jparse(got.body);
    if (obj == null) { sleep(1000); continue; }
    var cmd = obj.optJSONObject("cmd");
    if (cmd == null) {                        // 没活：正常空转
        idle++;
        if (idle % 15 === 0) console.log("…还在等活（" + idle + " 轮）");
        continue;
    }
    idle = 0;
    var id = cmd.optInt("id", 0);
    var kind = cmd.optString("kind", "");
    console.log("收到指令 #" + id + "： " + kind);
    var res, err = null;
    try {
        res = runCmd(cmd);
    } catch (e) {
        err = "" + e;
        res = { ok: false, error: err };
    }
    if (kind === "stop") { res = { ok: true, note: "收到停止指令，脚本退出" }; STOP = true; }
    var out = new org.json.JSONObject();
    out.put("id", id);
    out.put("ok", !!res.ok);
    out.put("data", new org.json.JSONObject(res).toString());
    if (res.error) out.put("error", "" + res.error);
    try {
        var r = httpReq("POST", "/api/ui/result", out.toString());
        if (r.code !== 200) console.warn("回执失败 HTTP " + r.code + "：" + r.body);
    } catch (e2) {
        console.warn("回执发不出去：" + e2);
    }
    if (res.error) console.warn("指令 #" + id + " 没做成：" + res.error);
    else console.log("指令 #" + id + " 完成");
}
toast("跑腿脚本已退出");
