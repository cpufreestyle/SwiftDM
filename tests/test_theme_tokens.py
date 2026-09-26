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
    "accent-ink": "onAccent",
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

# 扩展 popup 的调色板：变量名与 Web 端 :root 同名，
# 值必须与桌面 THEMES / Web CSS 变量逐个相同（None 表示那一端没有对应令牌）。
POPUP_TOKENS = {
    "surface": ("surface", "surface"),
    "surface2": ("surface2", "surface2"),
    "surface3": ("surface3", "surface3"),
    "text": ("text", "text"),
    "text2": ("text2", "textMuted"),
    "faint": (None, "faint"),
    "border": ("border", "border"),
    "accent": ("accent", "accent"),
    "accent-hover": ("accent-hover", "accentHover"),
    "accent2": ("accent2", "accent2"),
    "accent-ink": ("accent-ink", "onAccent"),
    "accent-soft": ("accent-soft", None),
    "green": ("green", "green"),
    "green-soft": ("green-soft", None),
    "red": ("red", "red"),
    "red-soft": ("red-soft", "redSoft"),
    "amber": ("amber", None),
    "amber-soft": ("amber-soft", None),
    # 只有 popup 才有的令牌：值显式写在 POPUP_ONLY，改它必须同时改测试
    "on-green": (None, None),
}

# popup 独有令牌的取值（深色 / 浅色）。写在这里是为了让「改颜色」
# 变成一次显式决定，而不是悄悄漂移。
POPUP_ONLY = {
    "on-green": {"dark": "#06231b", "light": "#06231b"},
}

# 正文 / 按钮字色 -> 它站在哪个背景上。两套主题都要达到 4.5:1（WCAG AA）。
# 类型标签（柔色底 + 饱和字）不在其中：那些值是从 Web 调色板整套继承来的，
# 要改就在那边改，popup 自动跟着变。
POPUP_TEXT_PAIRS = [
    ("accent-ink", "accent"),
    ("on-green", "green"),
    ("text", "surface"),
    ("text2", "surface"),
    ("text2", "surface2"),
]

# 类型标签 / 角标必须走「柔色底 + 饱和字」，且柔色来自共享令牌。
POPUP_CHIP_TINTS = {
    ".media-kind": ("--accent-soft", "--accent2"),
    ".media-kind.video": ("--green-soft", "--green"),
    ".media-kind.dash": ("--amber-soft", "--amber"),
    ".media-kind.mse": ("--red-soft", "--red"),
    ".badge": ("--green-soft", "--green"),
    ".badge.warn": ("--red-soft", "--red"),
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
    # 注释同样不含花括号，不削掉就会被算进前一条规则的选择器名里
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return {sel.strip(): body.strip() for sel, body in
            re.findall(r"([^{}]+)\{([^{}]*)\}", css)}


def _decl(rule, prop):
    """取某条规则里的某个属性值；找不到返回 None，便于区分「改丢了」。"""
    for name, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", rule):
        if name.lower() == prop:
            return value.strip()
    return None


def _lum(hexcode):
    """WCAG 相对亮度（#rrggbb）。"""
    channels = [int(hexcode[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
           for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    """两个颜色的对比度，无关顺序。"""
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
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


@pytest.fixture(scope="module")
def popup():
    # popup 的两套调色板 + 原文（查「还有没有写死的颜色」时要用）
    html = _read(os.path.join("extension", "popup.html"))
    return {"html": html,
            "dark": _css_block(html, ":root {"),
            "light": _css_block(html, ':root[data-theme="light"] {')}


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

@pytest.mark.parametrize("theme", ["dark", "light"])
def test_popup_palette_matches_shared_themes(popup, web, desktop, theme):
    """popup 的每个变量都必须与 Web / 桌面的同义令牌同值。"""
    for var, (web_key, desk_key) in POPUP_TOKENS.items():
        assert var in popup[theme], f"{theme}: popup 缺少 --{var}"
        if web_key:
            assert var in web[theme], f"{theme}: web 缺少 --{web_key}"
            assert popup[theme][var] == web[theme][web_key], (
                theme, var, popup[theme][var], web_key, web[theme][web_key])
        if desk_key:
            assert desk_key in desktop[theme], f"{theme}: 桌面缺少 {desk_key}"
            assert popup[theme][var] == desktop[theme][desk_key], (
                theme, var, popup[theme][var], desk_key, desktop[theme][desk_key])
        if var in POPUP_ONLY:
            assert popup[theme][var] == POPUP_ONLY[var][theme], (
                theme, var, popup[theme][var], POPUP_ONLY[var][theme])


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_popup_declares_only_registered_tokens(popup, theme):
    """新增 popup 变量必须登记进 POPUP_TOKENS，不允许悄悄只改弹窗。"""
    unknown = sorted(set(popup[theme]) - set(POPUP_TOKENS))
    assert not unknown, "popup 新变量未登记: %s" % unknown


def test_popup_has_no_literal_colors(popup):
    """调色板之外不允许再出现写死的颜色（Web 端早就这么做）。"""
    html = popup["html"]
    # 先扮掉 HTML 实体（&#11015; 这类数字实体含 #，不是颜色）
    html = re.sub(r"&[a-zA-Z#0-9]+;", "", html)
    css = html[html.index("<style>") + 7:html.index("</style>")]
    css = re.sub(r":root[^{]*\{[^}]*\}", "", css)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), (
        "popup 规则里还有写死的颜色")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", html[html.index("</style>"):]), (
        "popup 页面里还有写死的颜色")


def test_popup_chips_use_shared_tints(popup_rules, popup):
    """类型标签 / 角标必须走共享的柔色底 + 饱和字。"""
    for selector, (bg, fg) in POPUP_CHIP_TINTS.items():
        rule = popup_rules.get(selector)
        assert rule is not None, f"popup 里找不到 {selector} 规则"
        assert _decl(rule, "background") == "var(%s)" % bg, (
            selector, _decl(rule, "background"), bg)
        assert _decl(rule, "color") == "var(%s)" % fg, (
            selector, _decl(rule, "color"), fg)
    for bg, fg in POPUP_CHIP_TINTS.values():
        for var in (bg, fg):
            assert var[2:] in popup["dark"], var


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_popup_text_keeps_contrast(popup, theme):
    """同色背景配同色字比单级难看更严重：两套主题都要达到 4.5:1。"""
    for ink, ground in POPUP_TEXT_PAIRS:
        ratio = _contrast(popup[theme][ink], popup[theme][ground])
        assert ratio >= 4.5, (theme, ink, ground, round(ratio, 2))


def test_popup_theme_follows_the_app_setting():
    """popup 必须真的去问应用要主题，并且背景脚本要有 getSettings 分支。"""
    popup_js = _read(os.path.join("extension", "popup.js"))
    assert "document.documentElement.dataset.theme" in popup_js
    assert "'getSettings'" in popup_js, "popup.js 没有发 getSettings 消息"
    background = _read(os.path.join("extension", "background.js"))
    assert "message.action === 'getSettings'" in background
    assert "getJson('/api/settings')" in background
