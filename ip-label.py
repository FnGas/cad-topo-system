#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
相机 IP 与标签处理工具

导航菜单:
    1. 从 DXF 文件读取相机信息，输出 camera 的 label ↔ ip 对应关系（支持导出 CSV）
    2. 从 Excel / CSV 读取 label ↔ ip 映射，写入 DXF 文件补充或更新相机 IP

camera.xlsx / csv 结构:
    第 1 行: 标题行（如 label, ip，任意文字）
    A 列: label (相机编号/标签)
    B 列: ip    (相机 IP 地址)
"""

import csv
import json
import re
import sys
from pathlib import Path

# Windows 控制台编码 UTF-8 兜底
if sys.platform.startswith('win'):
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import ezdxf
    from ezdxf.lldxf.types import DXFTag
except ImportError:
    ezdxf = None
    DXFTag = None


# ==============================================================
# 基础工具
# ==============================================================
def check_ezdxf():
    """检查 ezdxf 依赖。"""
    if ezdxf is None:
        raise SystemExit('处理 DXF 文件需要 ezdxf 库，请先执行: pip install ezdxf')


def clean_path(raw: str) -> Path:
    """处理拖入路径：去掉引号、前后空白；支持 '&' 分隔的多个拖入（取第一个）。"""
    raw = raw.strip()
    if '&' in raw:                      # 多选拖入时 Windows 用 & 连接
        raw = raw.split('&')[0]
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    return Path(raw.strip())


def input_path(prompt: str, expect_ext: str = None) -> Path:
    """反复询问，直到拿到存在的文件；回车则取消。"""
    while True:
        raw = input(prompt).strip()
        if raw == '':
            print('  已取消')
            return None
        p = clean_path(raw)
        if not p.exists() or not p.is_file():
            print(f'  找不到文件: {p}，请重新拖入或输入路径')
            continue
        if expect_ext and p.suffix.lower() != expect_ext.lower():
            print(f'  文件类型错误: 请提供 {expect_ext} 文件（当前为 {p.suffix}）')
            continue
        return p


def load_mapping_file(path: Path) -> dict:
    """读取 Excel 或 CSV：第 1 行为表头，A 列 label，B 列 ip，第 2 行起为数据。"""
    if not path.exists():
        raise FileNotFoundError(f'文件不存在: {path}')

    ext = path.suffix.lower()
    mapping = {}
    duplicate = 0

    if ext in ('.xlsx', '.xlsm', '.xltx', '.xltm'):
        if openpyxl is None:
            raise RuntimeError('读取 .xlsx 文件需要 openpyxl 库，请先执行: pip install openpyxl')
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < 2:
                continue
            lbl, ip = row[0], row[1]
            if lbl is None or ip is None:
                continue
            lbl, ip = str(lbl).strip(), str(ip).strip()
            if not lbl or not ip:
                continue
            if lbl in mapping and mapping[lbl] != ip:
                duplicate += 1
            mapping[lbl] = ip

    elif ext == '.csv':
        encodings = ['utf-8-sig', 'utf-8', 'gbk', 'gb18030', 'latin1']
        content = None
        for enc in encodings:
            try:
                content = path.read_text(encoding=enc)
                break
            except Exception:
                continue
        if content is None:
            raise RuntimeError(f'无法以常见编码解析 CSV 文件: {path}')

        reader = csv.reader(content.splitlines())
        rows = list(reader)
        data_rows = rows[1:] if len(rows) > 1 else rows
        for row in data_rows:
            if not row or len(row) < 2:
                continue
            lbl, ip = str(row[0]).strip(), str(row[1]).strip()
            if not lbl or not ip:
                continue
            if lbl in mapping and mapping[lbl] != ip:
                duplicate += 1
            mapping[lbl] = ip
    else:
        raise RuntimeError(f'不支持的文件格式: {ext}，仅支持 .xlsx 和 .csv')

    if duplicate:
        print(f'  [提示] {duplicate} 个 label 在表格中出现多次，采用后出现的 ip')
    return mapping


# ==============================================================
# DXF 处理核心
# ==============================================================
def get_dxf_json_xrecords(doc):
    """
    检索 DXF 中的拓扑 JSON XRecord 对象。
    返回列表: [{"xrec": xrec_entity, "key": dict_key, "handle": handle, "data": json_dict}]
    """
    results = []
    handle_to_rootkey = {}
    if hasattr(doc, 'rootdict'):
        for k, v in doc.rootdict.items():
            if hasattr(v, 'dxf') and hasattr(v.dxf, 'handle'):
                handle_to_rootkey[v.dxf.handle] = k

    for obj in doc.objects:
        if obj.dxftype() != 'XRECORD':
            continue
        tags = getattr(obj, 'tags', [])
        chunks = [val for code, val in tags if code == 1]
        if not chunks:
            continue
        raw_text = ''.join(chunks)
        try:
            data = json.loads(raw_text)
            if isinstance(data, dict) and ('nodes' in data or 'version' in data):
                key = handle_to_rootkey.get(obj.dxf.handle)
                results.append({
                    'xrec': obj,
                    'key': key,
                    'handle': obj.dxf.handle,
                    'data': data,
                })
        except Exception:
            continue
    return results


def get_dxf_text_cameras(doc):
    """
    从 DXF 模型空间提取相机文本实体（TEXT / MTEXT）。
    支持 TOPO_CAMERA 图层或符合 camera 命名规则的文本。
    """
    cameras = []
    # 匹配 "LABEL" 或 "LABEL [IP]"
    pattern = re.compile(r'^(.*?)(?:\s*\[([0-9\.\:\w\-]+)\]|\s*\(([0-9\.\:\w\-]+)\))?$')

    for e in doc.modelspace():
        if e.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        layer = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
        text = e.text if e.dxftype() == 'MTEXT' else (e.dxf.text if hasattr(e.dxf, 'text') else '')
        text = text.strip()
        if not text:
            continue

        is_camera_layer = 'CAMERA' in layer.upper()
        m = pattern.match(text)
        if m:
            label = m.group(1).strip()
            ip = (m.group(2) or m.group(3) or '').strip()
            if is_camera_layer or ('CAM' in label.upper() or 'SC-' in label.upper()):
                cameras.append({
                    'entity': e,
                    'layer': layer,
                    'label': label,
                    'ip': ip,
                    'raw_text': text,
                    'handle': e.dxf.handle if hasattr(e.dxf, 'handle') else '',
                })
    return cameras


def read_dxf_camera_info(dxf_path: Path):
    """
    读取 DXF 文件的所有相机信息。
    优先读取 XRECORD 中的拓扑数据；若无则降级读取模型空间 TEXT 实体。
    """
    check_ezdxf()
    doc = ezdxf.readfile(dxf_path)
    xrecords = get_dxf_json_xrecords(doc)
    text_cameras = get_dxf_text_cameras(doc)

    return {
        'doc': doc,
        'xrecords': xrecords,
        'text_cameras': text_cameras,
    }


def write_dxf_camera_ip(doc, mapping: dict, overwrite_existing: bool = False):
    """
    将 mapping (label -> ip) 写入 DXF 的 XRECORD 和模型空间文本中。
    返回修改统计。
    """
    chunk_size = 240
    modified_nodes = 0
    modified_xrecords = 0
    modified_texts = 0
    details = []

    # 1. 更新 XRECORD 内部 JSON
    xrecords = get_dxf_json_xrecords(doc)
    for item in xrecords:
        obj = item['xrec']
        data = item['data']
        nodes = data.get('nodes', [])
        changed = False

        for node in nodes:
            if node.get('type') != 'camera':
                continue
            lbl = str(node.get('label', '')).strip()
            if not lbl or lbl not in mapping:
                continue

            target_ip = mapping[lbl]
            cur_ip = str(node.get('ip', '') or '').strip()

            # 决定是否更新
            if overwrite_existing or cur_ip == '':
                if cur_ip != target_ip:
                    node['ip'] = target_ip
                    changed = True
                    modified_nodes += 1
                    details.append((lbl, cur_ip, target_ip, 'XRECORD'))

        if changed:
            # 重新序列化并切分为 group 1 标签
            new_text = json.dumps(data, ensure_ascii=False)
            chunks = [new_text[i:i + chunk_size] for i in range(0, len(new_text), chunk_size)]
            other_tags = [t for t in getattr(obj, 'tags', []) if t.code != 1]
            obj.tags = other_tags + [DXFTag(1, c) for c in chunks]
            modified_xrecords += 1

    # 2. 更新模型空间中的文字标注（让 CAD 图纸视觉显示同步更新）
    for e in doc.modelspace():
        if e.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        cur_text = (e.text if e.dxftype() == 'MTEXT' else (e.dxf.text if hasattr(e.dxf, 'text') else '')).strip()

        for lbl, target_ip in mapping.items():
            pattern = re.compile(rf'^{re.escape(lbl)}(?:\s*\[.*?\]|\s*\(.*?\))?$')
            if pattern.match(cur_text):
                m_ip = re.search(r'\[(.*?)\]', cur_text)
                cur_ip = m_ip.group(1).strip() if m_ip else ''

                if overwrite_existing or cur_ip == '':
                    new_val = f'{lbl} [{target_ip}]' if target_ip else lbl
                    if cur_text != new_val:
                        if e.dxftype() == 'MTEXT':
                            e.text = new_val
                        else:
                            e.dxf.text = new_val
                        modified_texts += 1
                        details.append((lbl, cur_ip, target_ip, 'TEXT实体'))
                break

    return {
        'modified_nodes': modified_nodes,
        'modified_xrecords': modified_xrecords,
        'modified_texts': modified_texts,
        'details': details,
    }


# ==============================================================
# 功能 1：读取 DXF，输出 label ↔ ip 对应关系
# ==============================================================
def action_list(dxf_path: Path = None):
    print('\n--- 功能 1：从 DXF 文件读取相机信息 ---')
    if dxf_path is None:
        dxf_path = input_path('请将 DXF 文件拖到这里（或输入路径，回车取消）:\n> ', expect_ext='.dxf')
        if dxf_path is None:
            return

    if dxf_path.suffix.lower() != '.dxf':
        print(f'  [错误] 仅支持 DXF 文件，当前为: {dxf_path.suffix}')
        return

    try:
        info = read_dxf_camera_info(dxf_path)
    except Exception as e:
        print(f'  [错误] 读取 DXF 失败: {e}')
        return

    xrecords = info['xrecords']
    text_cameras = info['text_cameras']
    camera_list = []

    if xrecords:
        print(f'  成功读取 DXF，找到 {len(xrecords)} 个拓扑数据记录 (XRecord)')
        for idx, item in enumerate(xrecords, 1):
            key_name = item['key'] or '未命名'
            handle = item['handle']
            data = item['data']
            nodes = data.get('nodes', [])
            cams = [n for n in nodes if n.get('type') == 'camera']
            print(f'\n  === XRecord #{idx}: {key_name} (Handle: {handle}, 相机数: {len(cams)}) ===')
            print(f'  {"序号":<5s} {"label (名称)":<25s} {"ip 地址":<20s} 状态')
            print('  ' + '-' * 65)
            for c_idx, node in enumerate(cams, 1):
                lbl = str(node.get('label', '')).strip()
                ip = str(node.get('ip', '') or '').strip()
                status = '已配置' if ip else '未设置 [空]'
                print(f'  {c_idx:<5d} {lbl:<25s} {ip:<20s} {status}')
                camera_list.append((lbl, ip, f'XRecord:{key_name}'))

    elif text_cameras:
        print(f'  未找到 XRecord 拓扑记录，但从 DXF 图纸中找到 {len(text_cameras)} 个相机文本标注:')
        print(f'  {"序号":<5s} {"label (名称)":<25s} {"ip 地址":<20s} 图层')
        print('  ' + '-' * 65)
        for c_idx, tc in enumerate(text_cameras, 1):
            lbl, ip, lyr = tc['label'], tc['ip'], tc['layer']
            print(f'  {c_idx:<5d} {lbl:<25s} {ip:<20s} {lyr}')
            camera_list.append((lbl, ip, f'Layer:{lyr}'))
    else:
        print('  DXF 文件中未检测到相机数据或相机文本标注。')
        return

    # 统计信息
    dedup_cams = {}
    for lbl, ip, src in camera_list:
        if lbl not in dedup_cams or (not dedup_cams[lbl] and ip):
            dedup_cams[lbl] = ip

    filled_cnt = sum(1 for ip in dedup_cams.values() if ip)
    empty_cnt = len(dedup_cams) - filled_cnt
    print(f'\n  [统计] 共读取到 {len(dedup_cams)} 个独立相机: 已配置 IP: {filled_cnt} 个，未配置 IP: {empty_cnt} 个')

    # 导出 CSV
    ans = input('\n  是否导出相机列表为 CSV？(y/N): ').strip().lower()
    if ans == 'y':
        csv_path = dxf_path.with_suffix('.cameras.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['label', 'ip'])
            for lbl, ip in dedup_cams.items():
                w.writerow([lbl, ip])
        print(f'  已导出: {csv_path}')
        print('  (提示: 填好 IP 后，可直接在功能 2 中使用该 CSV 写入 DXF 文件)')


# ==============================================================
# 功能 2：读取 DXF + Excel/CSV，写入信息到 DXF
# ==============================================================
def action_fill(dxf_path: Path = None, mapping_path: Path = None):
    print('\n--- 功能 2：将相机 IP 写入 DXF 文件 ---')

    if dxf_path is None:
        dxf_path = input_path('先把 DXF 文件拖到这里（回车取消）:\n> ', expect_ext='.dxf')
        if dxf_path is None:
            return

    if dxf_path.suffix.lower() != '.dxf':
        print(f'  [错误] 仅支持 DXF 文件，当前为: {dxf_path.suffix}')
        return

    if mapping_path is None:
        mapping_path = input_path('再把 Excel 或 CSV 映射文件拖到这里（回车取消）:\n> ')
        if mapping_path is None:
            return

    # 读取表格
    try:
        mapping = load_mapping_file(mapping_path)
    except Exception as e:
        print(f'  [错误] 读取表格失败: {e}')
        return
    print(f'  从映射表格读到 {len(mapping)} 条 label → ip 对应关系')

    # 询问覆盖已有 IP 还是仅填补空 IP
    print('\n  写入模式:')
    print('    1) 仅填补空 IP（默认，不影响已设置 IP 的相机）')
    print('    2) 覆盖已有 IP（只要表格中存在，即全部更新）')
    strategy = input('  请选择模式 [1/2，默认 1]: ').strip() or '1'
    overwrite_existing = (strategy == '2')

    # 询问输出方式
    print('\n  输出保存方式:')
    print('    1) 覆盖原文件（自动备份原文件为 *.bak）')
    print('    2) 输出到新文件（默认 <原名>_updated.dxf）')
    mode = input('  请选择 [1/2，默认 2]: ').strip() or '2'

    if mode == '1':
        out_path = dxf_path
        backup_path = dxf_path.with_suffix(dxf_path.suffix + '.bak')
    else:
        out_path = dxf_path.with_name(f'{dxf_path.stem}_updated.dxf')
        backup_path = None

    # 执行写入 DXF
    check_ezdxf()
    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as e:
        print(f'  [错误] 打开 DXF 文件失败: {e}')
        return

    res = write_dxf_camera_ip(doc, mapping, overwrite_existing=overwrite_existing)
    modified_nodes = res['modified_nodes']
    modified_texts = res['modified_texts']
    details = res['details']

    if modified_nodes == 0 and modified_texts == 0:
        print('\n  没有需要修改的相机 IP，DXF 未改动。')
        return

    print(f'\n  --- 修改明细 (共修改 {len(details)} 项) ---')
    seen_details = set()
    for lbl, old_ip, new_ip, target_type in details:
        key = (lbl, new_ip)
        if key not in seen_details:
            seen_details.add(key)
            old_desc = f'"{old_ip}"' if old_ip else '空'
            print(f'  [写入] {lbl:<20s}: {old_desc} -> {new_ip} ({target_type})')

    print(f'\n  统计: 更新拓扑节点 {modified_nodes} 处，更新图纸文字标注 {modified_texts} 处')

    # 备份并保存
    if backup_path:
        backup_path.write_bytes(dxf_path.read_bytes())
        print(f'  原文件已备份为: {backup_path}')

    doc.saveas(out_path)
    print(f'  成功写入 DXF: {out_path}')


# ==============================================================
# 导航菜单 & 入口
# ==============================================================
MENU = """
============================================
  相机 IP 处理工具 (DXF 专用版)
============================================
  1) 从 DXF 文件读取相机信息 (输出 label ↔ ip)
  2) 将相机 IP 写入 DXF 文件 (根据 Excel/CSV)
  q) 退出
============================================
"""


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    # 支持命令行直接传参运行
    args = sys.argv[1:]
    if len(args) == 1:
        action_list(Path(args[0]))
        return
    elif len(args) >= 2:
        action_fill(Path(args[0]), Path(args[1]))
        return

    while True:
        print(MENU)
        choice = input('请选择 [1/2/q]: ').strip().lower()

        if choice == '1':
            action_list()
        elif choice == '2':
            action_fill()
        elif choice in ('q', 'quit', 'exit'):
            print('再见')
            break
        else:
            print('  无效输入，请重新选择')

        input('\n按回车返回菜单...')


if __name__ == '__main__':
    main()
