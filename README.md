# 合宙 IoT (AirCloud) · Home Assistant 自定义集成

[![Validate](https://github.com/JochenZhou/ha-aircloud/actions/workflows/validate.yml/badge.svg)](https://github.com/JochenZhou/ha-aircloud/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JochenZhou&repository=ha-aircloud&category=integration)

把**合宙 AirCloud / iot.openluat.com** 平台的定位设备（宠物追踪器、车载定位器、Air8202 等）
接入 Home Assistant：地图轨迹、电量、信号、卫星数、转向统计。

> HA custom integration for the **OpenLuat AirCloud** IoT platform — device tracker, battery,
> signal, satellite count and turn statistics for LuatOS GPS trackers.

---

## 一键安装

点上面的按钮，或直接点这个链接 → 会自动打开你 HA 里的 **HACS** 并定位到本仓库：

**① 添加到 HACS**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JochenZhou&repository=ha-aircloud&category=integration)

**② 重启 HA 后，一键添加集成**

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aircloud)

> 这两个链接用的是 Home Assistant 官方的 [My Home Assistant](https://my.home-assistant.io/) 跳转服务。
> 首次点会问一次你的 HA 地址（之后记住），**不会**把任何信息发给第三方 —— 跳转目标始终是你自己的
> HA 实例（`https://<你的HA>/_my_redirect/…`）。需要 HA **2022.8+**。
> 如果按钮点了没反应（比如 HA 只在局域网、浏览器在公网），用下面的手动步骤。

---

## 功能

| 平台 | 实体 | 说明 |
|---|---|---|
| `device_tracker` | 定位 | WGS84 经纬度、地址、上报时间（可直接上 HA 地图） |
| `sensor` | 电量 / 电压 / 4G 信号 / 可见卫星 / 速度 / 地址 / 最后上报 | |
| `sensor` | 轨迹点数 / 转向次数 | 属性含轨迹数组（已简化），可直接画线 |
| `sensor` | 自动登录 | 诊断：上次重登结果、失败次数、距下次重试秒数 |
| `binary_sensor` | 在线 / 低电量 / GPS 定位 | |

- **中英文双语界面**（跟随 HA 界面语言）
- **自带品牌图标**（openluat logo）
- **零外部依赖**：仅用 HA 自带库 + Python 标准库，无需 pycryptodome

## 安装

### 方式一：一键添加（推荐）

1. 点上面的 **[① 添加到 HACS]** 按钮 → HACS 打开本仓库 → 点 **Download** → **重启 HA**
2. 点 **[② 一键添加集成]** 按钮 → 填手机号密码即可（详见下方「配置」）

### 方式二：HACS 手动添加

1. HACS → 右上角 ⋮ → **自定义存储库**
2. 添加 `https://github.com/JochenZhou/ha-aircloud`，类别选 **集成（Integration）**
3. 搜索「合宙 IoT」→ 下载 → **重启 Home Assistant**
4. 设置 → 设备与服务 → 添加集成 → **合宙 IoT**

### 方式三：手动

把 `custom_components/aircloud/` 拷进 HA 的 `/config/custom_components/`，重启 HA。

## 配置

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aircloud)

1. 添加集成，填**合宙账号的手机号 + 密码**
2. **验证码识别模型**（可选）：
   - 选一个 → 集成自动取图、识别、提交；识别不准自动换图重试（最多 3 次）
   - 不选（默认）→ 下一步直接显示验证码图片，手动填写
3. 单项目自动跳过；多设备勾选要跟踪的
4. 之后在**集成 → 配置**里可改轮询间隔、跟踪设备、手机号/密码/识别模型

> ⚠️ **务必选择支持视觉（多模态）的模型**。纯文本模型看不了图，选了会导致自动识别全败
> （手动填仍可用，不影响登录）。推荐 `deepseek-v4.1-flash` 这类视觉模型。
> 本集成通过 **LLM Vision**（`llmvision`）集成调用模型，需先装好它并配置至少一个视觉 provider。

## 三个核心设计

### 1. 轨迹按轮询频率累积，不读取平台历史点位

集成**每轮只问一次「设备现在在哪」**（`latest_location`），把这个点并入滚动轨迹缓存。
轨迹的密度就是轮询间隔，长度是「集成启动以来设备真正走过的路」。

为什么不用 `location_history` 把历史点拉回来（曾这样做，已废弃）：

- 该接口**按时间升序分页**，一页 100 条。7 天窗口实测 **9687 条 / 97 页**，
  只翻固定几页拿到的永远是**最老的那一段**（实测翻 4 页 400 条全是 3 天前上午的点）。
- 于是缓存里攒的是几天前的旧坐标，而设备**当前**位置由轮询补进来 ——
  两者在图上直接连成一条**跨城 20 km 直线**，看起来就是「轨迹在乱跳」。
- 想翻完又会破坏频率约定：平台的闸门提示写得很明确 ——
  「查询频率应该近似等于设备上报频率」，狂翻历史只会排队变慢。

现在的口径（`15 s` 轮询实测 `latest_location` **8/8 成功**）：

| 项目 | 行为 |
|---|---|
| 每轮请求 | `latest_location`（定位）+ `list_by_tags`（电量/卫星/信号/定位标识） |
| 轨迹来源 | 每轮 1 个定位点，追加进缓存 |
| 静止去重 | 与**缓存末点同坐标**则跳过（设备静止 300 s 一报，会有重复轮次） |
| 缓存上限 | `TRACK_CACHE_MAX_POINTS = 480`（15 s 一报约 2 小时连续移动），超出裁掉最老的 |
| 重启表现 | 轨迹从零开始重新累积（**不含启动前的历史位置**） |

> 去重只跟**最后一个点**比，不做全局去重：轮询数据里坐标重复意味着设备
> **真的回到了同一位置**（原路返回、绕圈、停车场兜圈）。全局去重会把返程整段吃掉
> —— 实测原路来回 11 点 / 0.729 km，全局去重后只剩 0.364 km，正好少一半。

### 2. 轨迹点的清理与几何简化

设备静止时的 GPS 抖动、偶尔的折返尖刺会让折线毛糙，直接写进实体属性还会拖慢前端。
移植参考实现 [`luatos-pet-track`](https://github.com/JochenZhou/luatos-pet-track) 的
`js/algo/alg.js` v7：

1. **同位折叠** —— 停留时段（连续点都在 `merge_m` 内）折成一个节点。
2. **折返尖刺** —— 偏离路线数公里、停 1~2 个点、原路弹回。
3. **几何简化**（Douglas–Peucker）—— 把点数压到 `ENTITY_TRACK_MAX_POINTS` 以内。

前两步在原始序列上**预计算、删除不级联**。这点是照搬参考实现的关键：
按「单步速度上限」逐个判罚会把真实行程整条删光 —— 设备是「冻结-跳变」式更新，
坐标冻结数分钟后沿路线一次性跳 2~8 km，瞬时速度看着有几百 km/h，但那是真实位移。
参考实现早期版本据此回放，**831 点删剩 42 点**。必须保留沿线跳变（三点近似共线，
折返比值 ≈ 1）与真实调头（相邻点几百米，够不着 `spike_min`）。

| 参数 | 值 | 含义 |
|---|---|---|
| `TRACK_MERGE_M` | 150 m | 相邻同位折叠半径（停留时段折成一个节点） |
| `TRACK_SPIKE_MIN_M` | 1000 m | 折返尖刺的最小折离距离 |
| `TRACK_SPIKE_RATIO_K` | 2.5 | 折返形状判据 `dAB+dBC > K × max(dAC, 500m)` |
| `TRACK_SPIKE_V_MPS` | 55.6 | 200 km/h，进出至少一侧是瞬移才算尖刺 |
| `ENTITY_TRACK_MAX_POINTS` | 240 | 写进实体属性的点上限（几何简化，保首尾） |

**简化必须用 Douglas–Peucker，不能用等距抽稀**（踩过）。等距抽稀按点数硬切，
拐弯、密集段、来回往复全被抹平 —— 同一天 41 km 的轨迹抽到 60 点只剩 **22 km（少 45%）**，
折线偏离真实路线最多 **849 m**，地图上就是「一段一段跳」。Douglas–Peucker 在同一
点数下偏离只有 **4.8 m**、长度保留 **98.9%**。

**缓存与展示分开**（重要）：

- **缓存**保留每次轮询的原始点 → 转向统计与距离计算不受简化影响；
- **展示**清理 + 简化后才写进 `track` 属性 → 前端只收到两三百个点。

> 清理算法依赖**数值时间戳**。平台只给 `time` 字符串，缺 `ts` 时会被当成瞬移。
> 实测缺 `ts` 时「同秒多点」判据会把**整条轨迹**当成同一秒（12 点 → 1 点），
> 折返判据也会删掉正常往返（12 点 → 3 点）。集成在点入缓存前统一补 `ts`，
> 且 `ts` 不全时自动跳过与时间相关的判据。

- `sensor.轨迹点数`：state = 缓存点数；属性 `track` 是 `[lat,lng]` 数组
  （可直接给卡片画线）、`new_points_this_round`（本轮**真正并入缓存**的点数，设备没动
  就是 0）、`fetched_points_this_round`（本轮从平台取回的定位点数），以及清理口径
  `track_display_points` / `track_raw_points` / `track_dupes_removed` / `track_outliers_removed`
- `sensor.转向次数`：state = 累计转向数，属性含 `recent_turns_last40pts`

### 3. 转向判定必须过滤静止抖动

只用「相邻点方位角变化 > 25°」会把 GPS 抖动算成转弯（实测静止一晚误报 133 次）。
增加**最小分段位移**过滤（两段都 ≥ 15 m 才算转向）：

| 场景 | 阈值 | 转向数 |
|---|---|---|
| 移动时段（6.57 km / 308 点） | 15 m | **8**（与轨迹吻合） |
| 移动时段 | 30 m | 2（过度过滤） |
| 静止时段（0 位移 / 41 点） | 15 m | **0** ✅ |

## 单会话平台与自动重新登录

**平台每账号只允许一个有效会话**：在别处（手机 App、网页）登录，会让本集成的 token
立刻失效（平台错误码 105「用户在其它地方登录」）。

因此集成在条目里持久化 **手机号 + 密码 + 识别模型**，失效后**自己重新登录**，用户无感：

- 重登成功 → 就地换掉内存客户端并写回新 token，无需重启
- **临时失败**（识别没中、模型限流）→ `UpdateFailed`，每 **10 分钟**重试一次，集成保持可用
- **永久失败**（密码已错、没存密码、模型已不存在）→ 弹「需要重新认证」交回用户，
  避免反复重试撞账号锁定
- 依赖未就绪（HA 启动顺序竞态）→ 5 秒短重试，不计失败
- 诊断实体 `sensor.aircloud_auto_relogin` 显示当前状态与失败计数

如果希望 HA 与手机 App 长期并存，建议**给 HA 单开一个子账号**。

## 平台对接红线（实测）

- 必须 **POST** `/iot/open_api/*` + `Content-Type: application/json`（GET → nginx 404）
- 三鉴权头 `authorization` / `salt` / `sid`，**不能加 `Bearer` 前缀**
- 登录链路强制**图形验证码**，且服务端**先验验证码、后验密码**——无法绕过
  （匿名调用业务接口一律 code 13；仅 `list_my_projects` 匿名可返回 code 0）
- `X-Key-Open-Api` = RSA/ECB/PKCS1v1.5(时间戳ms, appId)，纯标准库即可实现
- 坐标优先用平台返回的 `wlng/wlat`（已是 WGS84）；自算 GCJ02→WGS84 需 3 次迭代
- 平台时间字面即北京时间，**禁止 ±8 换算**

## 验证码自动识别（实测）

| 重试次数 | 累计成功率 |
|---|---|
| 1 | 83% |
| 2 | 97.1% |
| **3（默认）** | **99.5%** |

调参记录：

- **原图 160×80 优于放大**：4× 放大到 640×320 反而降到 58%（模糊和干扰线一起被放大）
- **必须用唯一文件名**：LLM Vision 按文件名缓存结果，复用文件名会永远返回第一张图的识别结果
- 模型可能返回带空格的 `"g m R B"`，需过滤非 ASCII 字母数字后再取前 4 位
- 只保留 ASCII：`str.isalnum()` 对中文也返回 `True`，中文说明会被当成合法字符

## 已知限制

- 需要 **Home Assistant 2024.12+**
- 自动验证码识别依赖 **LLM Vision** 集成中的视觉模型；没有则退化为手动填写
- 平台为单会话，手机 App 与 HA 不能同时在线（见上）
- **轨迹只包含集成启动后轮询到的点**，看不到集成启动前的历史位置（重启 HA 后从零累积）。
  需要历史回放请用平台 App 或 [`luatos-pet-track`](https://github.com/JochenZhou/luatos-pet-track)

## 调试

集成使用标准 HA 日志：

```yaml
logger:
  logs:
    custom_components.aircloud: debug
```

## 声明

非官方集成，与合宙（LuatOS）官方无关。仅调用平台公开 HTTP 接口，未做任何破解。

## License

[MIT](LICENSE)
