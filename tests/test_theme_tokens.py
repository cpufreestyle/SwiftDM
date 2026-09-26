"""三个前端的主题令牌一致性守护：桌面 THEMES / Web CSS 变量 / 扩展 popup。

命名风格各不相同（桌面 camelCase、Web kebab-case、扩展直接写死颜色），
但语义相同的颜色必须是同一个值，否则用户切主题时会看到
「桌面一个色、浏览器一个色、扩展又是一个色」的割裂感。

这里不去统一命名（那是无意义改动），而是维护显式映射表：
任一端只改了自己那一侧，测试立刻失败，并指出是哪一条。
"""
import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Web CSS 变量名 -> 桌面 THEMES 键名（语义相同才登记）
PAIRS = {
    "bg": "bg",
    "surface": "surface",
    "surface2": "surface2",
    "surface3": "surface3",
    "border": "border",
    "border-strong": "borderHover",
    "text": "text",
    "text2": "textMuted",
    "accent": "accent",
    "accent-hover": "accentHover",
    "accent2": "accent2",
    "green": "green",
    "orange": "orange",
    "red": "red",
    "red-soft": "redSoft",
    "blue": "blue",
}

# 只有一端有的令牌：登记成白名单，新增令牌忘登记映射时测试会失败
WEB_ONLY = frozenset({
    "text3", "accent-soft", "green-strong", "green-hover", "green-soft",
    "amber", "amber-soft", "blue-strong", "blue-soft",
    "radius", "radius-sm", "shadow",
})
DESKTOP_ONLY = frozenset({
    "input", "toolbar", "hover", "selected", "textStrong", "faint",
    "scroll", "scrollHandle", "scrollHover", "logBg", "logFg",
    "orangeSoft", "orangeHover", "redText",
})

# 扩展 popup 只有一套深色皮肤，逐条断言它复用桌面 THEMES["dark"] 的同名令牌。
# 值是 linear-gradient / 1px solid 这类简写时，按颜色出现顺序对应多个令牌。
POPUP_MAP = {
    "body": [("background", "surface"), ("color", "text")],
    ".logo": [("background", ("accent", "accent2"))],
    ".status-row": [("background", "surface2")],
    ".status-dot.active": [("background", "green")],
    ".status-dot.inactive": [("background", "red")],
    ".stats": [("color", "textMuted")],
    ".footer": [("color", "faint"), ("border-top", "border")],
    ".btn-toggle": [("background", "accent")],
    ".btn-toggle:hover": [("background", "accentHover")],
    ".btn-reset": [("color", "red"), ("border", "red")],
    ".tab.active": [("background", "accent")],
}


def _read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _css_block(src, selector):
    start = src.index(selector)
    start = src.index("{", start)
    end = src.index("}", start)
    body = src[start + 1:end]
    return {k: v.strip().lower()
            for k, v in re.findall(r"--([a-zA-Z0-9-]+)\s*:\s*([^;]+);", body)}


def _py_block(src, theme):
    start = src.index('"%s": {' % theme)
    start = src.index("{", start)
    end = src.index("},", start)
    body = src[start + 1:end]
    return {k: v.lower() for k, v in
            re.findall(r"""["'](\w+)["']\s*:\s*["']([^"']+)["']""", body)}


def _style_rules(html, close_tag="</style>"):
    css = html[html.index("<style>") + 7:html.index(close_tag)]
    return {sel.strip(): body.strip() for sel, body in
            re.findall(r"([^{}]+)\{([^{}]*)\}", css)}


def _decl(rule, prop):
    """取某条规则里的某个属性值；找不到返回 None，便于区分「改丢了」。"""
    for name, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", rule):
        if name.lower() == prop:
            return value.strip()
    return None


def _colors(value):
    """从声明值里抠出颜色并归一成 6 位小写，#555 -> #555555。"""
    out = []
    for hexcode in re.findall(r"#[0-9a-fA-F]{3,6}\b", value):
        hexcode = hexcode.lower()
        if len(hexcode) == 4:
            hexcode = "#" + hexcode[1] * 2 + hexcode[2] * 2 + hexcode[3] * 2
        out.append(hexcode)
    return out


def _call_span(src, match):
    """取从匹配到的左括号到与之配对的右括号之间的文本。

    setStyleSheet 的参数经常换行（f-string 拼多行），只看单行会漏掉颜色字面量。
    必须传 match 而不是 opener 字符串：后者总会回到文件里第一个
    setStyleSheet，导致每个调用点都只扫到同一处。
    """
    start = match.end()
    depth, i = 1, start
    while i < len(src) and depth:
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
        i += 1
    return src[start:i]


def test_no_hardcoded_colors_in_desktop_stylesheets():
    """Web 端早就禁止在 CSS 里写死颜色，桌面端也要守住同一条线。

    合法异常只有 TRAY_ICON_INK 那个候选表（故意不随主题变），
    其余 setStyleSheet 只能出现令牌拼接，不能写死色。
    """
    src = _read("main_window.py")
    allowed = {"#ffffff", "#0d1117"}
    offenders = []
    for m in re.finditer(r"\.setStyleSheet\(", src):
        span = _call_span(src, m)
        for hexcode in re.findall(r"#[0-9a-fA-F]{3,6}\b", span):
            if hexcode not in allowed:
                offenders.append((hexcode, span.strip()[:60]))
    assert not offenders, offenders


def test_qss_template_has_no_hex_colors_either():
    """QSS_TEMPLATE 里除了白色（用于带色背景的按钮）不应出现其它死色。"""
    src = _read("main_window.py")
    opener = 'QSS_TEMPLATE = string.Template("""'
    start = src.index(opener) + len(opener)
    qss = src[start:src.index('""")', start)]
    leftovers = [c for c in re.findall(r"#[0-9a-fA-F]{3,6}\b", qss) if c.lower() != "#fff"]
    assert not leftovers, leftovers



@pytest.fixture(scope="module")
def web():
    html = _read(os.path.join("templates", "index.html"))
    return {"dark": _css_block(html, ":root {"),
            "light": _css_block(html, ':root[data-theme="light"] {')}


@pytest.fixture(scope="module")
def desktop():
    src = _read("main_window.py")
    return {t: _py_block(src, t) for t in ("dark", "light")}


@pytest.fixture(scope="module")
def popup_rules():
    return _style_rules(_read(os.path.join("extension", "popup.html")))


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_shared_tokens_agree_on_value(web, desktop, theme):
    for web_key, desk_key in PAIRS.items():
        assert web_key in web[theme], f"{theme}: web 缺少 --{web_key}"
        assert desk_key in desktop[theme], f"{theme}: desktop 缺少 {desk_key}"
        assert web[theme][web_key] == desktop[theme][desk_key], (
            theme, web_key, web[theme][web_key], desk_key,
            desktop[theme][desk_key])


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_token_is_accounted_for(web, desktop, theme):
    """新增令牌必须显式登记进映射或白名单，不允许悄悄只改一端。"""
    unmapped_web = set(web[theme]) - set(PAIRS) - WEB_ONLY
    assert not unmapped_web, "web 端新令牌未登记: %s" % sorted(unmapped_web)
    unmapped_desk = set(desktop[theme]) - set(PAIRS.values()) - DESKTOP_ONLY
    assert not unmapped_desk, "桌面端新令牌未登记: %s" % sorted(unmapped_desk)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_no_token_regressed_to_transparent_or_black(web, desktop, theme):
    """同色背景配同色文字的隐形文本，比单纯难看更严重，顺手兜住。"""
    for side, tokens in (("web", web[theme]), ("desktop", desktop[theme])):
        for key, value in tokens.items():
            assert value != "#000000", (side, theme, key)

# 扩展 popup 的媒体类型标签色：和 Web 端同名语义的染料必须同值，
# 否则同一个 HLS 视频在侧边栏和网页里看起来不一样。
POPUP_KIND_TINTS = {
    ".media-kind": ("accent-soft", "accent2"),
    ".media-kind.video": ("green-soft", "green"),
    ".media-kind.dash": ("amber-soft", "amber"),
    ".media-kind.mse": ("red-soft", "red"),
}


def test_extension_popup_kind_chips_match_web(popup_rules, web):
    dark = web["dark"]
    for selector, (bg_key, fg_key) in POPUP_KIND_TINTS.items():
        rule = popup_rules.get(selector)
        assert rule is not None, f"popup 里找不到 {selector} 规则"
        background, color = _decl(rule, "background"), _decl(rule, "color")
        assert background and color, f"{selector} 缺 background/color 声明"
        assert _colors(background) == [dark[bg_key]], (
            selector, "background", background, bg_key, dark[bg_key])
        assert _colors(color) == [dark[fg_key]], (
            selector, "color", color, fg_key, dark[fg_key])


def test_extension_popup_reuses_dark_palette(popup_rules, desktop):
    dark = desktop["dark"]
    for selector, decls in POPUP_MAP.items():
        rule = popup_rules.get(selector)
        assert rule is not None, f"popup 里找不到 {selector} 规则"
        for prop, tokens in decls:
            want = (tokens,) if isinstance(tokens, str) else tokens
            value = _decl(rule, prop)
            assert value is not None, f"{selector} 的 {prop} 属性丢了"
            got = _colors(value)
            assert len(got) == len(want), f"{selector} {prop} 的颜色数量变了: {value}"
            for token, color in zip(want, got):
                assert token in dark, f"桌面 THEMES[dark] 没有 {token}"
                assert color == dark[token], (
                    selector, prop, token, color, dark[token])
