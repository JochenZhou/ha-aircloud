"""Config flow for AirCloud.

接入只需合宙账号（手机号 + 密码）。图形验证码有两种处理方式，由用户在表单里选择：

  * 选了「识别模型」→ 集成自动取图、识别、提交；失败自动换图重试，
    最多 OCR_MAX_ATTEMPTS 次
  * 不选 → 直接把验证码图片显示在表单里，人工填写（同款做法见社区集成 xiaomi_miot）

验证码不可绕过：平台服务端**先验验证码、后验密码**（空验证码+错密码 → 报「验证码错误」；
有效验证码+错密码 → 报「密码错误(1)」），登录页 onSubmit 也硬校验 captcha。

界面文案要点：
  * 验证码图片通过占位符 ``captcha_block`` 注入（值本身就是一段 markdown），
    这样图取不到时可以退化成纯文字提示，**绝不会渲染成一个空 src 的破图**。
  * 平台返回的具体原因通过占位符 ``detail`` 追加在说明末尾。
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import AirCloudApi, AirCloudAuthError, AirCloudError
from .const import (
    CONF_APP_ID,
    CONF_CAPTCHA,
    CONF_DEVICES,
    CONF_OCR_PROVIDER,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PROJECT,
    CONF_PUBLIC_KEY,
    CONF_SALT,
    CONF_SCAN_INTERVAL,
    CONF_SID,
    CONF_TOKEN,
    DEFAULT_APP_ID,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OCR_MANUAL,
    OCR_MAX_ATTEMPTS,
)
from .login import (
    AirCloudCaptchaError,
    AirCloudLoginError,
    AirCloudPasswordError,
    async_captcha,
    async_login,
    async_oauth_params,
)
from .ocr import async_list_providers, async_read_captcha

_LOGGER = logging.getLogger(__name__)

_MANUAL = OCR_MANUAL
_MANUAL_LABEL = "不自动识别（下一步手动填写验证码）"
# 验证码图取不到时的兜底文案（避免 markdown 渲染空 src 破图）
_CAPTCHA_MISSING = "**验证码图片获取失败**，请点击下方「提交」按钮重新获取。"


async def _validate(
    hass, data: dict, project: str | None = None
) -> tuple[list[dict], list[str], str | None]:
    """返回 (项目列表, 设备列表, 错误信息)。"""
    api = AirCloudApi(
        session=async_get_clientsession(hass),
        token=data[CONF_TOKEN],
        salt=data[CONF_SALT],
        sid=data[CONF_SID],
        public_key=data.get(CONF_PUBLIC_KEY, ""),
        app_id=data.get(CONF_APP_ID) or DEFAULT_APP_ID,
    )
    try:
        projects = await api.async_list_projects()
    except AirCloudError as err:
        return [], [], f"接口异常：{err}"
    if not projects:
        # 关键：匿名调用也能返回 code 0，必须靠「项目/设备是否为空」判断凭据真伪
        return [], [], "该账号下没有任何项目，请确认手机号与密码"
    key = project or projects[0]["project_key"]
    try:
        payload = await api.async_list_devices(key, 1, 100)
    except AirCloudError as err:
        return projects, [], f"读取设备失败：{err}"
    device_ids = [
        rec.get("deviceid")
        for rec in (payload.get("records") or [])
        if rec.get("deviceid")
    ]
    if not device_ids:
        return projects, [], "该项目下没有设备"
    return projects, device_ids, None


class AirCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """配置流程：账号 → （自动/人工验证码）→ 选项目 → 选设备。"""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._projects: list[dict] = []
        self._devices: list[str] = []
        self._error_detail = ""
        self._captcha_id = ""
        self._captcha_img = ""
        self._oauth: dict[str, str] = {}
        self._phone = ""
        self._password = ""
        self._ocr_provider: dict[str, Any] | None = None
        # 识别模型在 provider 字典里的 key —— 会被存进条目，供自动重登复用
        self._provider_key: str = OCR_MANUAL
        self._providers: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # 共用工具
    # ------------------------------------------------------------------ #
    async def _ensure_oauth(self) -> None:
        if not self._oauth:
            self._oauth = await async_oauth_params(
                async_get_clientsession(self.hass)
            )

    async def _load_captcha(self, attempts: int = 3) -> str:
        """取一张新验证码图（失败自动重试），返回最后一次的错误信息。

        成功时 ``self._captcha_img`` 有值、返回空串；失败时两者都空。
        之所以要重试：取图接口偶发抖动时，旧实现会把空图塞进 markdown，
        界面上渲染成一个空 src 的破图 —— 用户看到的就是「没有验证码」。
        """
        last = ""
        for _ in range(max(1, attempts)):
            try:
                await self._ensure_oauth()
                cap_id, img = await async_captcha(
                    async_get_clientsession(self.hass)
                )
                if cap_id and img:
                    self._captcha_id, self._captcha_img = cap_id, img
                    return ""
                last = "验证码接口未返回图片"
            except Exception as err:  # noqa: BLE001
                last = str(err)
                # oauth 参数可能已失效，清掉让下一次重取
                self._oauth = {}
        self._captcha_id = ""
        self._captcha_img = ""
        return last

    def _detail_block(self) -> str:
        """把平台返回的具体原因包成一段（为空时什么都不显示）。"""
        return f"\n\n{self._error_detail}" if self._error_detail else ""

    def _captcha_placeholders(self, err: str = "") -> dict[str, str]:
        """验证码步骤的占位符。

        ``captcha_block`` 的值本身就是一段 markdown：有图就是图片，没图就是文字，
        所以界面上不会出现空 src 的破图。
        """
        if self._captcha_img:
            block = f"![验证码]({self._captcha_img})"
        else:
            block = _CAPTCHA_MISSING
            if err:
                self._error_detail = (
                    f"{self._error_detail}\n\n获取验证码失败：{err}".strip()
                )
        return {"detail": self._detail_block(), "captcha_block": block}

    async def _do_login(self, captcha: str) -> dict[str, str]:
        await self._ensure_oauth()
        return await async_login(
            async_get_clientsession(self.hass),
            self._oauth,
            self._phone,
            self._password,
            self._captcha_id,
            captcha,
        )

    def _account_schema(self) -> vol.Schema:
        """账号表单。有 provider 时附加「识别模型」下拉。"""
        fields: dict[Any, Any] = {
            vol.Required(CONF_PHONE, default=self._phone): str,
            # 已保存过密码时回填，用户重新登录（reauth）不必重打
            vol.Required(CONF_PASSWORD, default=self._password): str,
        }
        if self._providers:
            options = [{"value": _MANUAL, "label": _MANUAL_LABEL}] + [
                {"value": pid, "label": info["label"]}
                for pid, info in sorted(
                    self._providers.items(), key=lambda kv: kv[1]["label"]
                )
            ]
            default_provider = (
                self._provider_key if self._provider_key in self._providers else _MANUAL
            )
            fields[vol.Optional(CONF_OCR_PROVIDER, default=default_provider)] = SelectSelector(
                SelectSelectorConfig(
                    options=options, mode=SelectSelectorMode.DROPDOWN
                )
            )
        return vol.Schema(fields)

    def _captcha_schema(self) -> vol.Schema:
        return vol.Schema({vol.Required(CONF_CAPTCHA): str})

    async def _after_credentials(
        self, creds: dict[str, str], form_step: str = "user"
    ) -> FlowResult:
        """凭据就绪 → 唯一 ID 校验 → 选项目/设备。"""
        self._data = {
            CONF_TOKEN: creds["token"],
            CONF_SALT: creds["salt"],
            CONF_SID: creds["sid"],
            CONF_PUBLIC_KEY: creds["public_key"],
            CONF_APP_ID: creds["app_id"],
            # 手机号/密码/识别模型一并保存：平台单会话，token 随时会被别处登录踢掉，
            # 存了这三样集成才能自己重新登录（见 relogin.py），用户不用管
            CONF_PHONE: self._phone,
            CONF_PASSWORD: self._password,
            CONF_OCR_PROVIDER: self._provider_key,
        }
        projects, devices, error = await _validate(self.hass, self._data)
        if error:
            self._error_detail = error
            return self._show_credentials_form({"base": "invalid_auth"}, form_step)
        await self.async_set_unique_id(f"aircloud_{creds['sid']}")
        self._abort_if_unique_id_configured()
        self._projects = projects
        self._devices = devices
        if len(projects) == 1:
            self._data[CONF_PROJECT] = projects[0]["project_key"]
            return await self.async_step_devices()
        return await self.async_step_project()

    def _show_credentials_form(
        self, errors: dict[str, str], step_id: str
    ) -> FlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=self._account_schema(),
            errors=errors,
            description_placeholders={"detail": self._detail_block()},
        )

    # ------------------------------------------------------------------ #
    # 步骤一：账号（+ 可选自动识别）
    # ------------------------------------------------------------------ #
    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        self._providers = await async_list_providers(self.hass)
        if user_input is None:
            return self._show_credentials_form({}, "user")

        self._phone = user_input[CONF_PHONE]
        self._password = user_input[CONF_PASSWORD]
        chosen = str(user_input.get(CONF_OCR_PROVIDER, _MANUAL) or _MANUAL)
        self._provider_key = chosen
        self._ocr_provider = (
            None if chosen in (_MANUAL, "", None) else self._providers.get(chosen)
        )

        if self._ocr_provider:
            return await self._async_auto_ocr()

        # 未选择模型 → 显示验证码让人工填
        err = await self._load_captcha()
        return self.async_show_form(
            step_id="captcha",
            data_schema=self._captcha_schema(),
            errors={"base": "login_failed"} if err else {},
            description_placeholders=self._captcha_placeholders(err),
        )

    # ------------------------------------------------------------------ #
    # 步骤二：人工填写验证码
    # ------------------------------------------------------------------ #
    async def async_step_captcha(
        self, user_input: dict | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                creds = await self._do_login(user_input[CONF_CAPTCHA])
            except AirCloudCaptchaError as err:
                errors["base"] = "captcha_error"
                self._error_detail = str(err)
            except AirCloudPasswordError as err:
                errors["base"] = "invalid_auth"
                self._error_detail = str(err)
            except (AirCloudLoginError, Exception) as err:  # noqa: BLE001
                errors["base"] = "login_failed"
                self._error_detail = str(err)
            else:
                return await self._after_credentials(creds, "user")

        err = await self._load_captcha()
        if err:
            errors["base"] = "login_failed"
        return self.async_show_form(
            step_id="captcha",
            data_schema=self._captcha_schema(),
            errors=errors,
            description_placeholders=self._captcha_placeholders(err),
        )

    # ------------------------------------------------------------------ #
    # 自动识别（选了模型时）：取图 → OCR → 提交，失败换图重试
    # ------------------------------------------------------------------ #
    async def _async_auto_ocr(self) -> FlowResult:
        last_error = ""
        for attempt in range(1, OCR_MAX_ATTEMPTS + 1):
            err = await self._load_captcha()
            if err:
                last_error = f"获取验证码失败：{err}"
                break

            code = await async_read_captcha(
                self.hass, self._captcha_img, self._ocr_provider
            )
            if not code:
                last_error = "自动识别未取到结果"
                _LOGGER.debug("第 %s 次自动识别无结果", attempt)
                continue

            try:
                creds = await self._do_login(code)
            except AirCloudCaptchaError as err:
                last_error = str(err)
                _LOGGER.debug("第 %s 次识别结果 %s 未被接受", attempt, code)
                continue
            except AirCloudPasswordError as err:
                self._error_detail = str(err)
                return self._show_credentials_form(
                    {"base": "invalid_auth"}, "user"
                )
            except Exception as err:  # noqa: BLE001
                self._error_detail = str(err)
                return self._show_credentials_form(
                    {"base": "login_failed"}, "user"
                )
            else:
                _LOGGER.info("验证码自动识别成功（第 %s 次）", attempt)
                return await self._after_credentials(creds, "user")

        # 自动失败 → 退回人工填写，并**重新取一张新图**给用户看
        self._error_detail = (
            f"自动识别 {OCR_MAX_ATTEMPTS} 次均失败（{last_error}），请在下方手动填写。"
        )
        err = await self._load_captcha()
        return self.async_show_form(
            step_id="captcha",
            data_schema=self._captcha_schema(),
            errors={"base": "ocr_failed"},
            description_placeholders=self._captcha_placeholders(err),
        )

    # ------------------------------------------------------------------ #
    # 重新认证
    # ------------------------------------------------------------------ #
    async def async_step_reauth(self, entry_data: dict) -> FlowResult:
        """凭据失效（平台单会话：别处登录会踢掉本集成）。"""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._phone = (entry_data or {}).get(CONF_PHONE, "")
        self._provider_key = str((entry_data or {}).get(CONF_OCR_PROVIDER) or OCR_MANUAL)
        self._password = str((entry_data or {}).get(CONF_PASSWORD) or "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict | None = None
    ) -> FlowResult:
        self._providers = await async_list_providers(self.hass)
        if user_input is None:
            return self._show_credentials_form({}, "reauth_confirm")

        self._phone = user_input[CONF_PHONE]
        self._password = user_input[CONF_PASSWORD]
        chosen = str(user_input.get(CONF_OCR_PROVIDER, _MANUAL) or _MANUAL)
        self._provider_key = chosen
        self._ocr_provider = (
            None if chosen in (_MANUAL, "", None) else self._providers.get(chosen)
        )

        if self._ocr_provider:
            return await self._async_reauth_auto_ocr()

        err = await self._load_captcha()
        return self.async_show_form(
            step_id="reauth_captcha",
            data_schema=self._captcha_schema(),
            errors={"base": "login_failed"} if err else {},
            description_placeholders=self._captcha_placeholders(err),
        )

    async def async_step_reauth_captcha(
        self, user_input: dict | None = None
    ) -> FlowResult:
        entry = getattr(self, "_reauth_entry", None)
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                creds = await self._do_login(user_input[CONF_CAPTCHA])
            except AirCloudCaptchaError as err:
                errors["base"] = "captcha_error"
                self._error_detail = str(err)
            except AirCloudPasswordError as err:
                errors["base"] = "invalid_auth"
                self._error_detail = str(err)
            except Exception as err:  # noqa: BLE001
                errors["base"] = "login_failed"
                self._error_detail = str(err)
            else:
                return await self._finish_reauth(entry, creds)

        err = await self._load_captcha()
        if err:
            errors["base"] = "login_failed"
        return self.async_show_form(
            step_id="reauth_captcha",
            data_schema=self._captcha_schema(),
            errors=errors,
            description_placeholders=self._captcha_placeholders(err),
        )

    async def _async_reauth_auto_ocr(self) -> FlowResult:
        entry = getattr(self, "_reauth_entry", None)
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        last_error = ""
        for _attempt in range(1, OCR_MAX_ATTEMPTS + 1):
            err = await self._load_captcha()
            if err:
                last_error = f"获取验证码失败：{err}"
                break
            code = await async_read_captcha(
                self.hass, self._captcha_img, self._ocr_provider
            )
            if not code:
                last_error = "自动识别未取到结果"
                continue
            try:
                creds = await self._do_login(code)
            except AirCloudCaptchaError as err:
                last_error = str(err)
                continue
            except Exception as err:  # noqa: BLE001
                self._error_detail = str(err)
                return self._show_credentials_form(
                    {"base": "invalid_auth"}, "reauth_confirm"
                )
            else:
                return await self._finish_reauth(entry, creds)

        self._error_detail = (
            f"自动识别 {OCR_MAX_ATTEMPTS} 次均失败（{last_error}），请在下方手动填写。"
        )
        err = await self._load_captcha()
        return self.async_show_form(
            step_id="reauth_captcha",
            data_schema=self._captcha_schema(),
            errors={"base": "ocr_failed"},
            description_placeholders=self._captcha_placeholders(err),
        )

    async def _finish_reauth(
        self, entry: ConfigEntry, creds: dict[str, str]
    ) -> FlowResult:
        merged = {
            **entry.data,
            CONF_TOKEN: creds["token"],
            CONF_SALT: creds["salt"],
            CONF_SID: creds["sid"],
            CONF_PUBLIC_KEY: creds["public_key"],
            CONF_APP_ID: creds["app_id"],
            # 三要素继续保留，后续再被踢掉仍可自动重登
            CONF_PHONE: self._phone,
            CONF_PASSWORD: self._password,
            CONF_OCR_PROVIDER: self._provider_key,
        }
        _projects, _devices, error = await _validate(
            self.hass, merged, entry.data.get(CONF_PROJECT)
        )
        if error:
            self._error_detail = error
            return self._show_credentials_form(
                {"base": "invalid_auth"}, "reauth_confirm"
            )
        self.hass.config_entries.async_update_entry(entry, data=merged)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    # ------------------------------------------------------------------ #
    # 项目 / 设备
    # ------------------------------------------------------------------ #
    async def async_step_project(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._data[CONF_PROJECT] = user_input[CONF_PROJECT]
            _p, devices, error = await _validate(
                self.hass, self._data, self._data[CONF_PROJECT]
            )
            if error:
                return self.async_show_form(
                    step_id="project",
                    errors={"base": "cannot_connect"},
                    data_schema=self._project_schema(),
                )
            self._devices = devices
            return await self.async_step_devices()
        return self.async_show_form(
            step_id="project", data_schema=self._project_schema()
        )

    def _project_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_PROJECT): vol.In(
                    {
                        p["project_key"]: p.get("name") or p["project_key"]
                        for p in self._projects
                    }
                )
            }
        )

    async def async_step_devices(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            chosen = user_input[CONF_DEVICES]
            if isinstance(chosen, str):
                chosen = [chosen]
            self._data[CONF_DEVICES] = list(chosen)
            title = next(
                (
                    p.get("name")
                    for p in self._projects
                    if p["project_key"] == self._data[CONF_PROJECT]
                ),
                "",
            )
            return self.async_create_entry(
                title=f"{DEFAULT_NAME} · {title}" if title else DEFAULT_NAME,
                data=self._data,
            )
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICES, default=self._devices): cv.multi_select(
                        {d: d for d in self._devices}
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return AirCloudOptionsFlow()


class AirCloudOptionsFlow(OptionsFlow):
    """选项：轮询间隔 + 设备多选 + 登录凭据维护。

    凭据放在这里是为了方便：平台是单会话，token 随时会被别处登录踢掉，
    但只要有手机号 + 密码 + 识别模型，集成就会自己重新登录（见 relogin.py）。
    密码改了、想换识别模型，都在这里改，不用删掉集成重建。
    """

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        entry = self.config_entry
        cfg = {**entry.data, **(entry.options or {})}
        self._providers = await async_list_providers(self.hass)
        devices: list[str] = list(cfg.get(CONF_DEVICES) or [])

        if user_input is not None:
            picked = user_input[CONF_DEVICES]
            if isinstance(picked, str):
                picked = [picked]
            # 凭据写回 entry.data（不动 options），并顺带校验一次密码是否还对；
            # 只有真的填了新密码才校验，避免每次改轮询间隔都去登录一次
            new_password = str(user_input.get(CONF_PASSWORD) or "")
            new_phone = str(user_input.get(CONF_PHONE) or "").strip()
            new_provider = str(user_input.get(CONF_OCR_PROVIDER) or OCR_MANUAL)
            data = dict(entry.data)
            data[CONF_PHONE] = new_phone
            data[CONF_OCR_PROVIDER] = new_provider
            if new_password:
                data[CONF_PASSWORD] = new_password
            if data != entry.data:
                self.hass.config_entries.async_update_entry(entry, data=data)

            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    CONF_DEVICES: list(picked),
                }
            )

        api = AirCloudApi(
            session=async_get_clientsession(self.hass),
            token=cfg.get(CONF_TOKEN, ""),
            salt=cfg.get(CONF_SALT, ""),
            sid=cfg.get(CONF_SID, ""),
            public_key=cfg.get(CONF_PUBLIC_KEY, ""),
            app_id=cfg.get(CONF_APP_ID) or DEFAULT_APP_ID,
        )
        try:
            payload = await api.async_list_devices(cfg.get(CONF_PROJECT, ""), 1, 100)
            all_devices = [
                r.get("deviceid")
                for r in (payload.get("records") or [])
                if r.get("deviceid")
            ]
        except AirCloudAuthError:
            # 平台单会话，别处登录会把 token 踢掉。这里只影响「能选哪些设备」，
            # 不能因此让整个选项页 500 —— 回落到条目里已保存的设备列表。
            _LOGGER.warning("读取设备列表时登录态已失效，回落到已保存的设备列表")
            all_devices = devices
        except AirCloudError:
            all_devices = devices
        choices = sorted(set(all_devices) | set(devices))

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=int(cfg.get(CONF_SCAN_INTERVAL) or DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
            vol.Required(CONF_DEVICES, default=devices or choices): vol.All(
                vol.Coerce(list), vol.Length(min=1)
            ),
            vol.Optional(CONF_PHONE, default=str(cfg.get(CONF_PHONE) or "")): str,
            # 留空 = 不改密码（已保存的密码不会回显）
            vol.Optional(CONF_PASSWORD, default=""): str,
        }
        if self._providers:
            saved = str(cfg.get(CONF_OCR_PROVIDER) or OCR_MANUAL)
            options = [{"value": _MANUAL, "label": _MANUAL_LABEL}] + [
                {"value": pid, "label": info["label"]}
                for pid, info in sorted(
                    self._providers.items(), key=lambda kv: kv[1]["label"]
                )
            ]
            fields[vol.Optional(CONF_OCR_PROVIDER,
                                default=saved if saved in self._providers else _MANUAL)
                   ] = SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))
