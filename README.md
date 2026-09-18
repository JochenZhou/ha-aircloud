# 合宙 IoT (AirCloud) · Home Assistant 自定义集成

[![Validate](https://github.com/JochenZhou/ha-aircloud/actions/workflows/validate.yml/badge.svg)](https://github.com/JochenZhou/ha-aircloud/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

把**合宙 AirCloud / iot.openluat.com** 平台的定位设备（宠物追踪器、车载定位器、Air8202 等）
接入 Home Assistant：地图轨迹、电量、信号、卫星数、转向统计。

> HA custom integration for the **OpenLuat AirCloud** IoT platform — device tracker, battery,
> signal, satellite count and turn statistics for LuatOS GPS trackers.

---

## 功能

| 平台 | 实体 | 说明 |
|---|---|---|
| `device_tracker` | 定位 | WGS84 经纬度、地址、上报时间（可直接上 HA 地图） |
| `sensor` | 电量 / 电压 / 4G 信号 / 可见卫星 / 速度 / 地址 / 最后上报 | |
| `sensor` | 轨迹点数（本轮） / 转向次数（本轮） | 属性含完整轨迹数组，可直接画线 |
| `sensor` | 自动登录 | 诊断：上次重登结果、失败次数、距下次重试秒数 |
| `binary_sensor` | 在线 / 低电量 / GPS 定位 | |

- **中英文双语界面**（跟随 HA 界面语言）
- **自带品牌图标**（openluat logo）
- **零外部依赖**：仅用 HA 自带库 + Python 标准库，无需 pycryptodome

## 安装

### 方式一：HACS（推荐）

1. HACS → 右上角 ⋮ → **自定义存储库**
2. 添加 `https://github.com/JochenZhou/ha-aircloud`，类别选 **集成（Integration）**
3. 搜索「合宙 IoT」→ 下载 → **重启 Home Assistant**
4. 设置 → 设备与服务 → 添加集成 → **合宙 IoT**

### 方式二：手动

把 `custom_components/aircloud/` 拷进 HA 的 `/config/custom_components/`，重启 HA。

## 配置

1. 添加集成，填**合宙账号的手机号 + 密码**
2. **验证码识别模型**（可选）：
   - 选一个 → 集成自动取图、识别、提交；识别不准自动换图重试（最多 3 次）
   - 不选（默认）→ 下一步直接显示验证码图片，手动填写
3. 单项目自动跳过；多设备勾选要跟踪的
4. 之后在**集成 → 配置**里可改轮询间隔、跟踪设备、手机号/密码/识别模型

> ⚠️ **务必选择支持视觉（多模态）的模型**。纯文本模型看不了图，选了会导致自动识别全败
> （手动填仍可用，不影响登录）。推荐 `deepseek-v4.1-flash` 这类视觉模型。
> 本集成通过 **LLM Vision**（`llmvision`）集成调用模型，需先装好它并配置至少一个视觉 provider。

## 两个核心设计

### 1. 不丢点：用 `location_history` 回溯，而不是轮询 `latest_location`

实测设备**移动时 5 秒一报、静止时 300 秒一报**。固定 60 s 轮询在移动时只能拿到 1/12 的点，
拐弯、掉头这类瞬时动作全部丢失。

解法：每轮按「滑动时间窗」（起点 = 上轮结束时间 − 30 s 重叠）调 `location_history`，
取回窗口内**每一个点**，并入设备滚动轨迹缓存（去重、按时间排序、上限 240 点）。

实测依据：`location_history` **无频率闸门**（1 s 一次 6/6 成功、15 s 轮询 8/8 成功），
而 `latest_location` 1 s 一次约 50% 返回 429。所以走 history 既完整又不限流。

### 2. 转向判定必须过滤静止抖动

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
