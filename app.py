import os
import json
import math
import uuid
import re
import sqlite3
from datetime import datetime
from functools import wraps
from contextlib import contextmanager
from flask import Flask, render_template, request, jsonify, send_file, session
import ezdxf
from ezdxf import units

app = Flask(__name__)
app.secret_key = "cad-topo-secret-key-2026-enterprise"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "topo_system.db")
TOPO_APP_ID = "CAD_TOPO_SYSTEM"
TOPO_DATA_KEY = "CAD_TOPO_SYSTEM_DATA"


def read_topology(doc):
    """从 DXF 扩展字典读取独立于显示文字的完整拓扑属性。"""
    if TOPO_DATA_KEY not in doc.rootdict:
        return None
    record = doc.rootdict[TOPO_DATA_KEY]
    data = json.loads("".join(tag.value for tag in record.tags if tag.code == 1))
    if data.get("version") != 1:
        raise ValueError("不支持的拓扑数据版本")
    return data


def clear_topology_graphics(doc):
    """仅清除本系统标记过的图形，保留用户原有 CAD 实体。"""
    msp = doc.modelspace()
    for entity in list(msp):
        if entity.has_xdata(TOPO_APP_ID):
            msp.delete_entity(entity)
    if TOPO_DATA_KEY in doc.rootdict:
        del doc.rootdict[TOPO_DATA_KEY]


def read_legacy_topology(doc):
    """兼容旧版导出：按专用图层、文字样式及设备几何恢复可推断属性。"""
    msp = doc.modelspace()
    layer_types = {"TOPO_CAMERA": "camera", "TOPO_TERMINAL": "access",
                   "TOPO_NETWORK": "network", "TOPO_TEXT": "text"}
    nodes, links, consumed, radii = [], [], [], []
    for layer, kind in layer_types.items():
        entities = [e for e in msp if e.dxf.layer == layer]
        labels = [e for e in entities if e.dxftype() == "TEXT" and e.dxf.style == "TOPO_CN_STYLE"]
        anchors = []
        for e in entities:
            if kind == "camera" and e.dxftype() == "CIRCLE":
                anchors.append((e.dxf.center.x, e.dxf.center.y, e))
            elif kind in ("access", "network") and e.dxftype() == "LWPOLYLINE":
                pts = list(e.get_points("xy"))
                if len(pts) == 4 and pts[0][1] == pts[1][1] and pts[1][0] == pts[2][0]:
                    anchors.append((sum(p[0] for p in pts) / 4, sum(p[1] for p in pts) / 4, e))
        for text in labels:
            angle = 0
            if kind == "text":
                x, y = text.dxf.insert.x, text.dxf.insert.y
                label, ip, vlan = text.dxf.text, "", ""
            else:
                if not anchors:
                    continue
                anchor = min(anchors, key=lambda a: (a[0] - text.dxf.insert.x)**2 + (a[1] - text.dxf.insert.y)**2)
                anchors.remove(anchor)
                x, y, shape = anchor
                label, ip, vlan = text.dxf.text, "", ""
                match = re.search(r" \(([^()]*)\)$", label)
                if match:
                    vlan, label = match[1], label[:match.start()]
                match = re.search(r" \[([^\[\]]*)\]$", label)
                if match:
                    ip, label = match[1], label[:match.start()]
                radii.append(text.dxf.height / 0.95)
                if kind == "camera":
                    arcs = [e for e in entities if e.dxftype() == "ARC" and e.dxf.center.isclose(shape.dxf.center)]
                    if arcs:
                        arc = arcs[0]
                        angle = (arc.dxf.start_angle + (arc.dxf.end_angle - arc.dxf.start_angle) % 360 / 2) % 360
            nodes.append(dict(uid=uuid.uuid4().hex, type=kind, label=label, ip=ip, vlan=vlan, x=x, y=y, angle=angle))
        if labels and (kind == "text" or any(n["type"] == kind for n in nodes)):
            consumed.extend(entities)
    if not nodes:
        return None
    def endpoint(point):
        return next((n["uid"] for n in nodes if math.hypot(n["x"] - point.x, n["y"] - point.y) < 1e-5), None)
    for entity in msp:
        if entity.dxf.layer != "TOPO_LINK" or entity.dxftype() != "LINE":
            continue
        source, target = endpoint(entity.dxf.start), endpoint(entity.dxf.end)
        if source and target:
            midpoint = (entity.dxf.start + entity.dxf.end) / 2
            labels = [e for e in msp if e.dxf.layer == "TOPO_LINK" and e.dxftype() == "TEXT"
                      and e.dxf.style == "TOPO_CN_STYLE" and abs(e.dxf.insert.x - midpoint.x) < 1e-5
                      and abs(e.dxf.insert.y - midpoint.y - e.dxf.height / 0.9 / 0.95 * 0.3) < 1e-5]
            links.append(dict(uid=uuid.uuid4().hex, from_uid=source, to_uid=target,
                              cable_type="cat6", label=labels[0].dxf.text if labels else ""))
            consumed.extend([entity] + labels)
    for entity in consumed:
        if entity.is_alive:
            msp.delete_entity(entity)
    return {"version": 1, "nodes": nodes, "links": links, "icon_scale": 0.5,
            "legacy_radius": radii[0] if radii else None}

os.makedirs(UPLOAD_DIR, exist_ok=True)


# ==================== 1. SQLite 数据库初始化 ====================
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                dxf_filename TEXT NOT NULL,
                bounds_json TEXT NOT NULL,
                icon_scale REAL DEFAULT 0.5,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                uid TEXT NOT NULL,
                type TEXT NOT NULL,        -- camera / access(终端) / network / text
                label TEXT NOT NULL,
                ip TEXT DEFAULT '',
                vlan TEXT DEFAULT '',
                x REAL NOT NULL,
                y REAL NOT NULL,
                angle REAL DEFAULT 0,      -- 摄像头朝向角度 (0~360)
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                uid TEXT NOT NULL,
                from_uid TEXT NOT NULL,
                to_uid TEXT NOT NULL,
                cable_type TEXT DEFAULT 'cat6', -- cat6 / fiber / rvv
                label TEXT DEFAULT '',
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
        """)
        # 初始化默认密码：游客密码 123456，编辑密码 admin123
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('guest_pwd', '123456')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_pwd', 'admin123')")


init_db()


# ==================== 2. 权限认证装饰器与接口 ====================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") not in ("guest", "admin"):
            return jsonify({"error": "未登录或会话已过期，请先验证密码"}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            return jsonify({"error": "当前为游客只读模式，无权执行修改操作"}), 403
        return f(*args, **kwargs)
    return decorated


@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    return jsonify({"role": session.get("role", None)})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json() or {}
    role = data.get("role", "guest")
    pwd = data.get("password", "")

    with get_db() as conn:
        if role == "admin":
            row = conn.execute("SELECT value FROM settings WHERE key='admin_pwd'").fetchone()
            if row and row["value"] == pwd:
                session["role"] = "admin"
                return jsonify({"status": "ok", "role": "admin"})
            return jsonify({"error": "修改图纸密码错误！"}), 401
        else:
            row = conn.execute("SELECT value FROM settings WHERE key='guest_pwd'").fetchone()
            if row and row["value"] == pwd:
                session["role"] = "guest"
                return jsonify({"status": "ok", "role": "guest"})
            return jsonify({"error": "游客预览密码错误！"}), 401


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("role", None)
    return jsonify({"status": "ok"})


@app.route("/api/auth/change_password", methods=["POST"])
@admin_required
def change_password():
    data = request.get_json() or {}
    old_admin_pwd = data.get("old_admin_pwd", "")
    new_guest_pwd = data.get("new_guest_pwd", "").strip()
    new_admin_pwd = data.get("new_admin_pwd", "").strip()

    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='admin_pwd'").fetchone()
        if not row or row["value"] != old_admin_pwd:
            return jsonify({"error": "原编辑密码验证失败！"}), 400

        if new_guest_pwd:
            conn.execute("UPDATE settings SET value=? WHERE key='guest_pwd'", (new_guest_pwd,))
        if new_admin_pwd:
            conn.execute("UPDATE settings SET value=? WHERE key='admin_pwd'", (new_admin_pwd,))

    return jsonify({"status": "ok", "message": "密码修改成功！"})


# ==================== 3. DXF 解析引擎 ====================
def iter_dxf_entities(msp, max_depth=2):
    """递归展开模型空间实体及 INSERT 块引用中的几何图元"""
    def _walk(entities, depth):
        for e in entities:
            dxftype = e.dxftype()
            if dxftype == "INSERT" and depth < max_depth:
                try:
                    yield from _walk(e.virtual_entities(), depth + 1)
                except Exception:
                    pass
            else:
                yield e

    yield from _walk(msp, 0)


def parse_dxf_to_json(filepath, max_entities=60000):
    """读取 DXF 文件并提取前端 Canvas 渲染所需的轻量级几何数据与边界框（自动过滤孤立飞点）"""
    doc = ezdxf.readfile(filepath)
    msp = doc.modelspace()

    primitives = []
    xs, ys = [], []

    def update_bounds(x, y):
        if math.isfinite(x) and math.isfinite(y):
            xs.append(x)
            ys.append(y)

    count = 0
    for e in iter_dxf_entities(msp):
        if count >= max_entities:
            break
        t = e.dxftype()
        try:
            if t == "LINE":
                x1, y1 = e.dxf.start.x, e.dxf.start.y
                x2, y2 = e.dxf.end.x, e.dxf.end.y
                update_bounds(x1, y1)
                update_bounds(x2, y2)
                primitives.append({"t": "L", "p": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)]})
                count += 1

            elif t == "LWPOLYLINE":
                pts = []
                with e.points("xy") as points:
                    for x, y in points:
                        update_bounds(x, y)
                        pts.extend([round(x, 2), round(y, 2)])
                if len(pts) >= 4:
                    primitives.append({"t": "P", "p": pts, "c": 1 if e.closed else 0})
                    count += 1

            elif t == "POLYLINE":
                pts = []
                for v in e.vertices:
                    x, y = v.dxf.location.x, v.dxf.location.y
                    update_bounds(x, y)
                    pts.extend([round(x, 2), round(y, 2)])
                if len(pts) >= 4:
                    primitives.append({"t": "P", "p": pts, "c": 1 if e.is_closed else 0})
                    count += 1

            elif t == "CIRCLE":
                cx, cy, r = e.dxf.center.x, e.dxf.center.y, e.dxf.radius
                update_bounds(cx - r, cy - r)
                update_bounds(cx + r, cy + r)
                primitives.append({"t": "C", "p": [round(cx, 2), round(cy, 2), round(r, 2)]})
                count += 1

            elif t == "ARC":
                cx, cy, r = e.dxf.center.x, e.dxf.center.y, e.dxf.radius
                sa, ea = e.dxf.start_angle, e.dxf.end_angle
                update_bounds(cx - r, cy - r)
                update_bounds(cx + r, cy + r)
                primitives.append({"t": "A", "p": [round(cx, 2), round(cy, 2), round(r, 2), round(sa, 1), round(ea, 1)]})
                count += 1

            elif t in ("TEXT", "MTEXT"):
                if t == "TEXT":
                    txt = e.dxf.text
                    ins = e.dxf.insert
                    h = getattr(e.dxf, "height", 280)
                else:
                    txt = e.plain_text()
                    ins = e.dxf.insert
                    h = getattr(e.dxf, "char_height", 280)
                if txt and txt.strip():
                    x, y = ins.x, ins.y
                    update_bounds(x, y)
                    primitives.append({"t": "T", "p": [round(x, 2), round(y, 2), round(h, 2)], "s": txt.strip()[:40]})
                    count += 1
        except Exception:
            continue

    if not xs:
        min_x, min_y, max_x, max_y = 0, 0, 10000, 8000
    else:
        xs.sort()
        ys.sort()
        n = len(xs)
        if n >= 200:
            lo = int(n * 0.01)
            hi = max(int(n * 0.99) - 1, lo)
            min_x, max_x = xs[lo], xs[hi]
            min_y, max_y = ys[lo], ys[hi]
        else:
            min_x, max_x = xs[0], xs[-1]
            min_y, max_y = ys[0], ys[-1]

    bounds = {
        "min_x": round(min_x, 2),
        "min_y": round(min_y, 2),
        "max_x": round(max_x, 2),
        "max_y": round(max_y, 2),
        "width": round(max(max_x - min_x, 100), 2),
        "height": round(max(max_y - min_y, 100), 2),
    }
    return primitives, bounds


# ==================== 4. 示例 CAD 底图生成器 ====================
def create_sample_floorplan_dxf(filepath):
    """生成一张标准的办公楼层平面图 DXF，方便直接测试全流程"""
    doc = ezdxf.new("R2010")
    doc.units = units.MM
    msp = doc.modelspace()

    if "CN_STYLE" not in doc.styles:
        doc.styles.add("CN_STYLE", font="simhei.ttf")

    doc.layers.add("WALL", color=7)
    doc.layers.add("ROOM_TEXT", color=8)

    # 外墙 (24m x 14m, 单位 mm)
    msp.add_lwpolyline([(0, 0), (24000, 0), (24000, 14000), (0, 14000)], close=True, dxfattribs={"layer": "WALL"})
    # 走廊隔墙 (Y=5500 和 Y=8500)
    msp.add_line((0, 5500), (24000, 5500), dxfattribs={"layer": "WALL"})
    msp.add_line((0, 8500), (24000, 8500), dxfattribs={"layer": "WALL"})

    # 上排房间隔断
    for x in [4000, 9000, 16000]:
        msp.add_line((x, 8500), (x, 14000), dxfattribs={"layer": "WALL"})

    # 下排房间隔断
    for x in [8000, 13000]:
        msp.add_line((x, 0), (x, 5500), dxfattribs={"layer": "WALL"})

    # 房间名称标注
    rooms = [
        ("1F 弱电间(IDF)", 1200, 11500),
        ("财务室", 5600, 11500),
        ("研发办公区", 11200, 11500),
        ("一号大会议室", 18500, 11500),
        ("中央公共走廊", 10500, 6800),
        ("1F 前台大厅(出入口)", 2500, 2600),
        ("部门经理室", 9500, 2600),
        ("综合办公区", 17000, 2600),
    ]
    for name, rx, ry in rooms:
        msp.add_text(name, dxfattribs={"layer": "ROOM_TEXT", "style": "CN_STYLE", "height": 450, "insert": (rx, ry)})

    doc.saveas(filepath)


# ==================== 5. Web 路由与 API ====================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/projects", methods=["GET"])
@login_required
def list_projects():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.*,
                   (SELECT COUNT(*) FROM nodes WHERE project_id = p.id) AS node_count,
                   (SELECT COUNT(*) FROM links WHERE project_id = p.id) AS link_count
            FROM projects p ORDER BY p.updated_at DESC
        """).fetchall()
        return jsonify([dict(r) for r in rows])


@app.route("/api/projects/demo", methods=["POST"])
@admin_required
def create_demo_project():
    filename = f"demo_{uuid.uuid4().hex[:8]}.dxf"
    filepath = os.path.join(UPLOAD_DIR, filename)
    create_sample_floorplan_dxf(filepath)
    _, bounds = parse_dxf_to_json(filepath)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, dxf_filename, bounds_json, icon_scale, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("1F 办公区安防与网络点位图(示例)", filename, json.dumps(bounds), 0.8, now, now),
        )
        pid = cur.lastrowid

        demo_nodes = [
            ("n_sw1", "network", "1F-接入交换机-SW01", "192.168.10.254", "VLAN10/20", 2000, 10200, 0),
            ("n_cam1", "camera", "CAM-01 前台枪机", "192.168.20.11", "VLAN20", 1200, 4600, 325),
            ("n_cam2", "camera", "CAM-02 走廊东半球", "192.168.20.12", "VLAN20", 22500, 7000, 180),
            ("n_acc1", "access", "TERM-01 大厅考勤门禁终端", "192.168.30.101", "VLAN30", 800, 1200, 0),
            ("n_txt1", "text", "说明：摄像机统一走POE供电(VLAN20)", "", "", 14500, 6800, 0),
        ]
        for uid, t, label, ip, vlan, x, y, angle in demo_nodes:
            conn.execute(
                "INSERT INTO nodes (project_id, uid, type, label, ip, vlan, x, y, angle) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, uid, t, label, ip, vlan, x, y, angle),
            )

        demo_links = [
            ("l_1", "n_sw1", "n_cam1", "cat6", ""),
            ("l_2", "n_sw1", "n_cam2", "cat6", ""),
            ("l_3", "n_sw1", "n_acc1", "cat6", ""),
        ]
        for uid, f_uid, t_uid, ctype, lbl in demo_links:
            conn.execute(
                "INSERT INTO links (project_id, uid, from_uid, to_uid, cable_type, label) VALUES (?,?,?,?,?,?)",
                (pid, uid, f_uid, t_uid, ctype, lbl),
            )

    return jsonify({"id": pid, "message": "示例项目已生成"})


@app.route("/api/projects/upload", methods=["POST"])
@admin_required
def upload_dxf():
    file = request.files.get("file")
    name = request.form.get("name", "").strip()
    if not file or not file.filename.lower().endswith(".dxf"):
        return jsonify({"error": "请上传 .dxf 格式的 CAD 图纸文件"}), 400

    if not name:
        name = os.path.splitext(file.filename)[0]

    safe_filename = f"{uuid.uuid4().hex[:10]}.dxf"
    filepath = os.path.join(UPLOAD_DIR, safe_filename)
    file.save(filepath)

    try:
        doc = ezdxf.readfile(filepath)
        topology = read_topology(doc)
        if topology is None:
            topology = read_legacy_topology(doc)
        if topology is not None:
            clear_topology_graphics(doc)
            doc.saveas(filepath)
        _, bounds = parse_dxf_to_json(filepath)
        if topology and topology.get("legacy_radius"):
            topology["icon_scale"] = topology["legacy_radius"] / (max(bounds["width"], bounds["height"]) * 0.0025)
    except Exception as e:
        os.remove(filepath)
        return jsonify({"error": f"DXF 解析失败: {str(e)}"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, dxf_filename, bounds_json, icon_scale, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, safe_filename, json.dumps(bounds), topology.get("icon_scale", 0.5) if topology else 0.5, now, now),
        )
        pid = cur.lastrowid
        if topology is not None:
            for n in topology["nodes"]:
                conn.execute(
                    "INSERT INTO nodes (project_id, uid, type, label, ip, vlan, x, y, angle) VALUES (?,?,?,?,?,?,?,?,?)",
                    (pid, n["uid"], n["type"], n.get("label", ""), n.get("ip", ""), n.get("vlan", ""), n["x"], n["y"], n.get("angle", 0)),
                )
            for link in topology["links"]:
                conn.execute(
                    "INSERT INTO links (project_id, uid, from_uid, to_uid, cable_type, label) VALUES (?,?,?,?,?,?)",
                    (pid, link["uid"], link["from_uid"], link["to_uid"], link.get("cable_type", "cat6"), link.get("label", "")),
                )

    return jsonify({"id": pid, "name": name})


@app.route("/api/projects/<int:pid>", methods=["GET"])
@login_required
def get_project(pid):
    with get_db() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if not proj:
            return jsonify({"error": "项目不存在"}), 404

        nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes WHERE project_id = ?", (pid,)).fetchall()]
        links = [dict(r) for r in conn.execute("SELECT * FROM links WHERE project_id = ?", (pid,)).fetchall()]

        filepath = os.path.join(UPLOAD_DIR, proj["dxf_filename"])
        primitives, bounds = parse_dxf_to_json(filepath)

        conn.execute("UPDATE projects SET bounds_json = ? WHERE id = ?", (json.dumps(bounds), pid))

    return jsonify({
        "project": dict(proj),
        "bounds": bounds,
        "cad_primitives": primitives,
        "nodes": nodes,
        "links": links,
    })


@app.route("/api/projects/<int:pid>/save", methods=["POST"])
@admin_required
def save_project(pid):
    data = request.get_json() or {}
    nodes = data.get("nodes", [])
    links = data.get("links", [])
    icon_scale = float(data.get("icon_scale", 0.5))
    project_name = data.get("name")

    seen_ips = set()
    for n in nodes:
        ip_val = (n.get("ip") or "").strip()
        if ip_val:
            if ip_val in seen_ips:
                return jsonify({"error": f"保存失败：检测到重复的 IP 地址 {ip_val}"}), 400
            seen_ips.add(ip_val)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        if project_name:
            conn.execute("UPDATE projects SET name=?, icon_scale=?, updated_at=? WHERE id=?", (project_name, icon_scale, now, pid))
        else:
            conn.execute("UPDATE projects SET icon_scale=?, updated_at=? WHERE id=?", (icon_scale, now, pid))

        conn.execute("DELETE FROM nodes WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM links WHERE project_id = ?", (pid,))

        for n in nodes:
            conn.execute(
                "INSERT INTO nodes (project_id, uid, type, label, ip, vlan, x, y, angle) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, n["uid"], n["type"], n.get("label", ""), (n.get("ip") or "").strip(), n.get("vlan", ""), float(n["x"]), float(n["y"]), float(n.get("angle", 0))),
            )

        for l in links:
            conn.execute(
                "INSERT INTO links (project_id, uid, from_uid, to_uid, cable_type, label) VALUES (?,?,?,?,?,?)",
                (pid, l["uid"], l["from_uid"], l["to_uid"], l.get("cable_type", "cat6"), l.get("label", "")),
            )

    return jsonify({"status": "ok", "updated_at": now})


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
@admin_required
def delete_project(pid):
    with get_db() as conn:
        proj = conn.execute("SELECT dxf_filename FROM projects WHERE id = ?", (pid,)).fetchone()
        if proj:
            fpath = os.path.join(UPLOAD_DIR, proj["dxf_filename"])
            if os.path.exists(fpath):
                os.remove(fpath)
        conn.execute("DELETE FROM nodes WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM links WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    return jsonify({"status": "deleted"})


# ==================== 6. 导出为 CAD (.dxf) ====================
@app.route("/api/projects/<int:pid>/export_dxf", methods=["GET"])
@admin_required
def export_dxf(pid):
    """在原始 DXF 图纸基础上创建独立弱电图层，绘入点位图块、拓扑连线与文字并导出"""
    show_ip = request.args.get("show_ip", "1") == "1"
    # 前端传入当前可见图层；未传入时保持兼容，默认全部导出。
    layer_param = request.args.get("layers")
    visible_layers = set(layer_param.split(",")) if layer_param else {"cad", "camera", "access", "network", "link", "text"}

    with get_db() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if not proj:
            return "Project not found", 404
        nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes WHERE project_id = ?", (pid,)).fetchall()]
        links = [dict(r) for r in conn.execute("SELECT * FROM links WHERE project_id = ?", (pid,)).fetchall()]

    src_path = os.path.join(UPLOAD_DIR, proj["dxf_filename"])
    doc = ezdxf.readfile(src_path)
    clear_topology_graphics(doc)
    msp = doc.modelspace()
    if "cad" not in visible_layers:
        # 关闭 CAD 底图时不把原始图纸实体带入导出文件。
        for entity in list(msp):
            msp.delete_entity(entity)

    style_name = "TOPO_CN_STYLE"
    if style_name not in doc.styles:
        doc.styles.add(style_name, font="simhei.ttf")

    layer_defs = {
        "TOPO_CAMERA": 1,     # 摄像头图层 - 红色
        "TOPO_TERMINAL": 2,   # 终端图层   - 黄色
        "TOPO_NETWORK": 3,    # 网络设备层 - 绿色
        "TOPO_LINK": 4,       # 拓扑线缆层 - 青色
        "TOPO_TEXT": 6,       # 文字标注层 - 洋红
    }
    for lname, col in layer_defs.items():
        if lname not in doc.layers:
            doc.layers.add(lname, color=col)

    bounds = json.loads(proj["bounds_json"])
    span = max(bounds.get("width", 10000), bounds.get("height", 10000))
    scale = float(proj["icon_scale"] or 0.5)
    r = span * 0.0025 * scale
    text_h = r * 0.95

    node_map = {n["uid"]: n for n in nodes}
    base_handles = {entity.dxf.handle for entity in msp}

    # 1. 绘制拓扑连线
    if "link" in visible_layers:
        for l in links:
            n1 = node_map.get(l["from_uid"])
            n2 = node_map.get(l["to_uid"])
            if not n1 or not n2:
                continue
            x1, y1 = n1["x"], n1["y"]
            x2, y2 = n2["x"], n2["y"]
            msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "TOPO_LINK"})

            if l.get("label"):
                mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                msp.add_text(
                    l["label"],
                    dxfattribs={"layer": "TOPO_LINK", "style": style_name, "height": text_h * 0.9, "insert": (mx, my + r * 0.3)},
                )

    # 2. 绘制设备点位与文字标注
    for n in nodes:
        x, y = n["x"], n["y"]
        ntype = n["type"]
        if ntype not in visible_layers:
            continue
        label = n["label"]
        full_label = label
        if show_ip and n.get("ip"):
            full_label += f" [{n['ip']}]"
        if n.get("vlan"):
            full_label += f" ({n['vlan']})"

        if ntype == "camera":
            layer = "TOPO_CAMERA"
            msp.add_circle((x, y), radius=r * 0.6, dxfattribs={"layer": layer})
            # 用圆弧加两条半径构成扇形轮廓，避免导出为三角形。
            rad = math.radians(n.get("angle", 0))
            cone_len = r * 1.9
            spread = math.radians(28)
            start_deg = math.degrees(rad - spread)
            end_deg = math.degrees(rad + spread)
            p1 = (x + cone_len * math.cos(rad - spread), y + cone_len * math.sin(rad - spread))
            p2 = (x + cone_len * math.cos(rad + spread), y + cone_len * math.sin(rad + spread))
            msp.add_arc((x, y), radius=cone_len, start_angle=start_deg, end_angle=end_deg, dxfattribs={"layer": layer})
            msp.add_line((x, y), p1, dxfattribs={"layer": layer})
            msp.add_line((x, y), p2, dxfattribs={"layer": layer})
            msp.add_text(full_label, dxfattribs={"layer": layer, "style": style_name, "height": text_h, "insert": (x + r * 0.95, y - text_h * 0.4)})

        elif ntype == "access":
            layer = "TOPO_TERMINAL"
            s = r * 0.75
            msp.add_lwpolyline([(x - s, y - s), (x + s, y - s), (x + s, y + s), (x - s, y + s)], close=True, dxfattribs={"layer": layer})
            msp.add_lwpolyline([(x, y - s * 0.7), (x + s * 0.7, y), (x, y + s * 0.7), (x - s * 0.7, y)], close=True, dxfattribs={"layer": layer})
            msp.add_text(full_label, dxfattribs={"layer": layer, "style": style_name, "height": text_h, "insert": (x + s * 1.25, y - text_h * 0.4)})

        elif ntype == "network":
            layer = "TOPO_NETWORK"
            w, h = r * 1.1, r * 0.75
            msp.add_lwpolyline([(x - w, y - h), (x + w, y - h), (x + w, y + h), (x - w, y + h)], close=True, dxfattribs={"layer": layer})
            msp.add_line((x - w, y - h), (x + w, y + h), dxfattribs={"layer": layer})
            msp.add_line((x - w, y + h), (x + w, y - h), dxfattribs={"layer": layer})
            msp.add_text(full_label, dxfattribs={"layer": layer, "style": style_name, "height": text_h, "insert": (x + w * 1.25, y - text_h * 0.4)})

        elif ntype == "text":
            layer = "TOPO_TEXT"
            msp.add_text(label, dxfattribs={"layer": layer, "style": style_name, "height": text_h * 1.3, "insert": (x, y)})

    # 图形用于 CAD 显示，扩展数据用于系统再次导入；隐藏 IP 不影响属性保存。
    if TOPO_APP_ID not in doc.appids:
        doc.appids.add(TOPO_APP_ID)
    for entity in msp:
        if entity.dxf.handle not in base_handles:
            entity.set_xdata(TOPO_APP_ID, [(1000, "topology")])
    exported_nodes = [n for n in nodes if n["type"] in visible_layers]
    exported_uids = {n["uid"] for n in exported_nodes}
    exported_links = [link for link in links if "link" in visible_layers
                      and link["from_uid"] in exported_uids and link["to_uid"] in exported_uids]
    payload = json.dumps({"version": 1, "icon_scale": scale,
                          "nodes": exported_nodes, "links": exported_links}, ensure_ascii=True)
    record = doc.rootdict.add_xrecord(TOPO_DATA_KEY)
    record.reset([(1, payload[i:i + 240]) for i in range(0, len(payload), 240)])

    out_path = os.path.join(UPLOAD_DIR, f"export_{pid}.dxf")
    doc.saveas(out_path)

    download_name = f"{proj['name']}_拓扑导出.dxf"
    return send_file(out_path, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    print("启动成功！请在浏览器访问: http://127.0.0.1:5000")
    print("默认游客预览密码: 123456 | 默认修改图纸密码: admin123")
    app.run(host="0.0.0.0", port=5000, debug=True)
