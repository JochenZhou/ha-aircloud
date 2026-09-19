"""轨迹点位清理 —— 移植自参考实现 luatos-pet-track `js/algo/alg.js` 的 v7 综合版。

为什么要清理：原始点位里有几类垃圾，直接画出来既有视觉噪声又拖慢前端 ——

1. **补传副本**：设备把缓存位置整段补传，坐标与之前完全相同（连浮点都一样），
   轨迹被原样画两遍。**只对平台历史流成立** —— 本集成改为按轮询频率累积定位点后，
   坐标重复意味着设备真的回到了同一位置，此时必须
   ``global_dedupe=False``，否则返程会被整段吃掉（见 ``filter_track_outliers``）。
2. **同秒多点**：同一时刻上报多个相距甚远的位置（时标异常/补传交错）。
3. **折返尖刺**：偏离路线数公里、停 1~2 个点、原路弹回，出去/回来瞬时几百上千 km/h。

关键设计（照搬参考实现，勿改）：

- **三步全部在原始序列上预计算，删除不级联**。按「单步/锚点速度上限」逐个判罚会把
  真实行程整条删光：设备是「冻结-跳变」式更新，坐标冻结数分钟后沿路线一次性跳
  2~8 km，瞬时速度看上去有几百 km/h，但那是真实位移。参考实现早期版本据此回放，
  831 点删剩 42 点，留下 13 段共 1382 km 的直线。
- **沿线跳变必须保留**：三点近似共线时 `dAB + dBC ≈ dAC`，折返比值 ≈ 1，判据天然放过。
- **真实调头/绕行必须保留**：相邻点几百米，够不着 `spike_min`。
"""
from __future__ import annotations

import math

# 折返尖刺判据（与参考实现同值）
SPIKE_MIN_M = 1000.0      # 折离得足够远才值得管
RATIO_K = 2.5             # B 点「出去又回来」的形状判据
MERGE_M = 150.0           # 相邻同位折叠半径（停留时段折成一个节点）
V_SPIKE_MPS = 55.6        # 200 km/h：进出至少一侧是瞬移才算尖刺
SAME_SECOND_MS = 1000     # 同秒多点的时间容差
SAME_SECOND_FAR_M = 500.0 # 同秒点相距超过此值才需要取舍
# 全局坐标去重的量化位数（6 位 ≈ 0.1 m）：
# history 流是全精度浮点、tags 流是 6 位小数字符串，同一位置跨源精度不同，
# 精确相等会漏判，必须量化后再比。
ROUND_DIGITS = 6

# --- 几何简化（Douglas–Peucker）---
# 简化容差：只删「删掉后形状偏差小于该值」的点，拐点一个不丢。
SIMPLIFY_TOL_M = 5.0
# 逐步放宽的容差阶梯 —— 先用最细容差试，点数仍超预算再逐档放宽。
# 这样只要数据允许就保持最高保真度，绝不为了凑点数而乱切。
SIMPLIFY_LADDER_M = (5.0, 10.0, 15.0, 25.0, 50.0, 80.0, 120.0, 200.0)


def dist_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点球面距离（米）。"""
    radius = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lng / 2) ** 2)
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _coord(pt: dict) -> tuple[float, float] | None:
    try:
        return float(pt["lng"]), float(pt["lat"])
    except (KeyError, TypeError, ValueError):
        return None


def _ts_ms(pt: dict) -> float:
    """点的毫秒时间戳；缺失或非法返回 0（判据会把它当成「瞬移」）。"""
    ts = pt.get("ts")
    if ts is None:
        ts = pt.get("ts_ms")
    if ts is None:
        return 0.0
    try:
        return float(ts) * 1000.0
    except (TypeError, ValueError):
        return 0.0


def filter_track_outliers(points: list[dict], *,
                          spike_min: float = SPIKE_MIN_M,
                          ratio_k: float = RATIO_K,
                          merge_m: float = MERGE_M,
                          v_spike: float = V_SPIKE_MPS,
                          global_dedupe: bool = True,
                          stats: dict | None = None) -> list[dict]:
    """清理轨迹异常点（三步，删除不级联）。

    入参 ``points`` 需按时间升序，元素须含 ``lng`` / ``lat``；
    ``ts``（秒）可选，缺失时该侧的进出会被视为瞬移。

    ``global_dedupe`` 控制第 1 步（全局坐标去重）。**轮询累积的轨迹必须传 False**：
    那种数据里的坐标重复不再是「补传副本」，而是设备**真的回到了同一位置**
    —— 全局去重会把返程整段吃掉（实测原路来回 11 点、0.729 km，
    去重后只剩 0.364 km，正好少了返程那半）。
    只有消费平台历史流（同一段被补传多次）时才需要打开。

    返回清理后的**新列表**（不修改入参）。``stats`` 可选，回填
    ``{dropped, kept, dupes}``：``dropped`` = 尖刺 + 非法点，
    ``dupes`` = 补传去重数（二者分开便于 UI 区分口径）。
    """
    dropped = 0
    dupes = 0
    out: list[dict] = []
    if not points:
        if stats is not None:
            stats.update(dropped=0, kept=0, dupes=0)
        return out

    # --- 1) 全局坐标去重：完全相同的坐标只留首次出现（补传副本全删）。
    #        GPS 重新定位不可能产出完全相同的浮点坐标，只有复制品才会。
    #        注意：轮询累积的数据要传 global_dedupe=False（见 docstring）。
    seen: set[str] = set()
    uniq: list[dict] = []
    for pt in points:
        c = _coord(pt)
        if c is None:
            dropped += 1
            continue
        key = f"{round(c[0], ROUND_DIGITS)}|{round(c[1], ROUND_DIGITS)}"
        if global_dedupe and key in seen:
            dupes += 1
            continue
        seen.add(key)
        uniq.append(pt)

    # --- 1.5) 同秒多点取舍：同一时刻多个相距甚远的位置，折返检验只会删「中间」点、
    #          留「末端」点；若同秒两点排序颠倒，会把在线点删掉、留下离线点。
    #          这里按「与前后点衔接总距离最小」挑一个保留，其余删除。
    #          必须 `base_ts > 0`：缺 ts 时 _ts_ms 返回 0，若不判就会把**整条轨迹**
    #          当成「同一秒」并只留一个点（实测 12 点 → 1 点，轨迹全丢）。
    tidy: list[dict] = []
    i = 0
    while i < len(uniq):
        j = i + 1
        base_ts = _ts_ms(uniq[i])
        while (base_ts > 0 and j < len(uniq)
               and abs(_ts_ms(uniq[j]) - base_ts) <= SAME_SECOND_MS):
            j += 1
        if j - i >= 2:
            far = False
            for x in range(i, j):
                if far:
                    break
                cx = _coord(uniq[x])
                if cx is None:
                    continue
                for y in range(x + 1, j):
                    cy = _coord(uniq[y])
                    if cy is None:
                        continue
                    if dist_m(cx[0], cx[1], cy[0], cy[1]) > SAME_SECOND_FAR_M:
                        far = True
                        break
            if not far:
                tidy.extend(uniq[i:j])
            else:
                prev_ref = tidy[-1] if tidy else None
                next_ref = uniq[j] if j < len(uniq) else None
                best, best_score = -1, math.inf
                for x in range(i, j):
                    cx = _coord(uniq[x])
                    if cx is None:
                        continue
                    score = 0.0
                    for ref in (prev_ref, next_ref):
                        if ref is None:
                            continue
                        cr = _coord(ref)
                        if cr is None:
                            continue
                        score += dist_m(cr[0], cr[1], cx[0], cx[1])
                    if score < best_score:
                        best_score, best = score, x
                for x in range(i, j):
                    if x != best:
                        dropped += 1
                        continue
                    tidy.append(uniq[x])
        else:
            tidy.append(uniq[i])
        i = j
    uniq = tidy

    # --- 2) 相邻同位折叠：距上一节点代表点 <= merge_m 的连续点并入该节点（停留时段）。
    #        节点记录 uniq 的下标区间 [i0, i1]，便于回标删除。
    nodes: list[dict] = []
    for idx, pt in enumerate(uniq):
        c = _coord(pt)
        if c is None:
            continue
        last = nodes[-1] if nodes else None
        if last is not None:
            lc = _coord(last["repr"])
            if lc is not None and dist_m(lc[0], lc[1], c[0], c[1]) <= merge_m:
                last["t_end"] = _ts_ms(pt)
                last["i1"] = idx
                continue
        nodes.append({"repr": pt, "t0": _ts_ms(pt), "t_end": _ts_ms(pt),
                      "i0": idx, "i1": idx})

    # --- 3) 折返尖刺检验（预计算节点标记，不级联）
    #        缺 ts 时不能判「瞬移」—— _ts_ms 回落到 0 会让相邻两点的时间差变成 0
    #        或负数，进而被当成瞬移，把正常的往返段整段删掉（实测缺 ts 的
    #        原路来回 12 点被删到只剩 3 点）。ts 不全时只保留折返形状判据。
    ts_reliable = all(_ts_ms(p) > 0 for p in uniq)
    drop_node: set[int] = set()
    for idx in range(1, len(nodes) - 1):
        a, b, c = nodes[idx - 1], nodes[idx], nodes[idx + 1]
        ca, cb, cc = _coord(a["repr"]), _coord(b["repr"]), _coord(c["repr"])
        if ca is None or cb is None or cc is None:
            continue
        d1 = dist_m(ca[0], ca[1], cb[0], cb[1])
        d2 = dist_m(cb[0], cb[1], cc[0], cc[1])
        d_ac = dist_m(ca[0], ca[1], cc[0], cc[1])
        if max(d1, d2) <= spike_min:
            continue
        if d1 + d2 <= ratio_k * max(d_ac, 500.0):
            continue
        if not ts_reliable:
            continue
        dt1 = (b["t0"] - a["t_end"]) / 1000.0
        dt2 = (c["t0"] - b["t_end"]) / 1000.0
        fast1 = not dt1 > 0 or d1 / dt1 > v_spike
        fast2 = not dt2 > 0 or d2 / dt2 > v_spike
        if fast1 or fast2:
            drop_node.add(idx)

    # --- 汇总输出：被标节点连同其折叠点全部剔除
    node_idx = -1
    for idx, pt in enumerate(uniq):
        if node_idx < len(nodes) - 1 and idx == nodes[node_idx + 1]["i0"]:
            node_idx += 1
        if node_idx >= 0 and node_idx in drop_node:
            dropped += 1
            continue
        out.append(pt)

    if stats is not None:
        stats.update(dropped=dropped, kept=len(out), dupes=dupes)
    return out


def _seg_dist_m(pt: dict, a: dict, b: dict) -> float:
    """点 ``pt`` 到线段 ``ab`` 的最短距离（米）。"""
    ax, ay = float(a["lng"]), float(a["lat"])
    bx, by = float(b["lng"]), float(b["lat"])
    px, py = float(pt["lng"]), float(pt["lat"])
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return dist_m(px, py, ax, ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return dist_m(px, py, ax + t * dx, ay + t * dy)


def simplify_track(points: list[dict], tol_m: float = SIMPLIFY_TOL_M) -> list[dict]:
    """Douglas–Peucker 几何简化：只删「删掉后形状偏差小于 ``tol_m``」的点。

    为什么不能用等距抽稀（``points[::stride]``）：那相当于把轨迹按点数硬切，
    拐弯、密集段、来回往复全被抹掉。实测同一天 41 km 的轨迹，等距抽稀到 60 点
    只剩 22 km（少 45%），折线偏离真实路线最多 **848 m** —— 地图上就是
    「一段一段跳」。而 Douglas–Peucker 在同一点数下偏离只有 **4.8 m**，
    且累计长度基本不变（40.6 km / 41.0 km）。

    参考实现 `views2.js` 平时并不抽稀（只有 >20000 点才等距抽），因为它的
    点数上限来自 1294 GNSS 展开后的高密度；本集成没有那一路数据，
    只能靠几何简化来同时满足「形状保真」与「点数可控」。

    保留首尾点（Douglas–Peucker 天然如此）。
    """
    total = len(points)
    if total < 3:
        return list(points)
    keep = [False] * total
    keep[0] = keep[-1] = True
    stack = [(0, total - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        worst, worst_idx = 0.0, -1
        for k in range(i + 1, j):
            d = _seg_dist_m(points[k], points[i], points[j])
            if d > worst:
                worst, worst_idx = d, k
        if worst > tol_m and worst_idx > 0:
            keep[worst_idx] = True
            stack.append((i, worst_idx))
            stack.append((worst_idx, j))
    return [p for p, k in zip(points, keep) if k]


def thin_track(points: list[dict], max_points: int) -> list[dict]:
    """把点数压到 ``max_points`` 以内 —— **几何简化，等距抽稀只做兜底**。

    为什么不能用等距抽稀（``points[::stride]``）：那相当于把轨迹按点数硬切，
    拐弯、密集段、来回往复全被抹掉。实测同一天 41 km 的轨迹，等距抽稀到 60 点
    只剩 22 km（少 45%），折线偏离真实路线最多 **849 m** —— 地图上就是
    「一段一段跳」。几何简化在同一点数下偏离只有 **40 m**、累计长度保留 94.7%。

    流程：按 :data:`SIMPLIFY_LADDER_M` 从最细容差逐档放宽做 Douglas–Peucker，
    点数落进上限就返回 —— 保真度是「在预算内能做到的最好」；
    万一轨迹复杂度确实超出预算，才退化为等距抽稀（保留首尾）。

    **不要为了省 CPU 在这里加等距预抽稀**（踩过）：预抽稀本身就是几何破坏，
    实测把容差尺子废掉 —— 801 点先抽到 240 再 RDP，长度只保留 65.4%、
    偏离 804 m（与不做任何简化的旧实现一样糟）。而且没必要：缓存在
    ``coordinator._merge_track`` 里已被限长，输入最多几百点，全量 RDP
    只要约 10 ms。
    """
    if max_points <= 0:
        return []
    total = len(points)
    if total <= max_points:
        return list(points)

    simplified: list[dict] = list(points)
    for tol in SIMPLIFY_LADDER_M:
        candidate = simplify_track(points, tol)
        simplified = candidate
        if len(candidate) <= max_points:
            return candidate
        if len(candidate) < 3:
            return candidate
    # 兜底：几何简化仍超预算（轨迹过于复杂）→ 等距抽稀，仍保留首尾
    stride = math.ceil(len(simplified) / max_points)
    thinned = simplified[::stride]
    if thinned and thinned[-1] is not simplified[-1]:
        thinned.append(simplified[-1])
    return thinned
