from media_service import MediaRegistry, normalize_media_url


def _item(url, **kw):
    d = {"url": url, "kind": "hls"}
    d.update(kw)
    return d


def test_normalize_drops_fragment_and_tracking_keys_and_sorts_query():
    got = normalize_media_url("https://c.example.com/v/a.m3u8?b=2&a=1&spm=x#frag")
    assert got == "https://c.example.com/v/a.m3u8?a=1&b=2"


def test_normalize_lowercases_scheme_and_host_only():
    assert normalize_media_url("https://CDN.Example.com/V/A.M3U8") == "https://cdn.example.com/V/A.M3U8"


def test_normalize_keeps_signed_params():
    u = "https://c/x.m3u8?sign=abc&ts=1"
    assert normalize_media_url(u) == "https://c/x.m3u8?sign=abc&ts=1"


def test_record_dedups_same_normalized_url_within_tab():
    r = MediaRegistry()
    added = r.record("7", "https://site/v", "标题",
                     [_item("https://c/x.m3u8?a=1"), _item("https://c/x.m3u8?a=2&spm=y")])
    assert added == 1
    assert len(r.list_for_tab("7")) == 1


def test_record_merges_richer_metadata_into_existing_item():
    r = MediaRegistry()
    r.record("7", "https://site/v", "", [_item("https://c/x.m3u8", quality_hint="", bytes=0)])
    r.record("7", "https://site/v", "新标题",
             [_item("https://c/x.m3u8", quality_hint="1080P", bytes=4096)])
    items = r.list_for_tab("7")
    assert items[0]["quality_hint"] == "1080P"
    assert items[0]["bytes"] == 4096
    assert items[0]["title"] == "新标题"


def test_page_change_replaces_tab_items():
    r = MediaRegistry()
    r.record("7", "https://site/a", "A", [_item("https://c/a.m3u8")])
    r.record("7", "https://site/b", "B", [_item("https://c/b.m3u8")])
    urls = [it["url"] for it in r.list_for_tab("7")]
    assert urls == ["https://c/b.m3u8"]


def test_per_tab_cap_evicts_oldest():
    r = MediaRegistry(max_per_tab=3)
    r.record("7", "p", "", [_item(f"https://c/{i}.m3u8") for i in range(5)])
    assert [it["url"] for it in r.list_for_tab("7")] == [
        "https://c/2.m3u8", "https://c/3.m3u8", "https://c/4.m3u8"]


def test_total_cap_evicts_oldest_tab_first():
    now = [1000.0]
    r = MediaRegistry(max_per_tab=2, max_total=3, clock=lambda: now[0])
    r.record("1", "p1", "", [_item("https://c/1a.m3u8"), _item("https://c/1b.m3u8")])
    now[0] += 1
    r.record("2", "p2", "", [_item("https://c/2a.m3u8"), _item("https://c/2b.m3u8")])
    assert r.count() == 3
    assert [it["url"] for it in r.list_for_tab("1")] == ["https://c/1b.m3u8"]
    assert [it["url"] for it in r.list_for_tab("2")] == ["https://c/2a.m3u8", "https://c/2b.m3u8"]


def test_tab_expires_after_ttl():
    now = [500.0]
    r = MediaRegistry(clock=lambda: now[0], tab_ttl=60.0)
    r.record("9", "p", "", [_item("https://c/x.m3u8")])
    now[0] += 61
    assert r.list_for_tab("9") == []
    assert r.count() == 0


def test_media_id_is_stable_across_tabs_and_refresh():
    r = MediaRegistry()
    r.record("1", "p1", "", [_item("https://c/x.m3u8")])
    r.record("2", "p2", "", [_item("https://c/x.m3u8")])
    assert r.list_for_tab("1")[0]["media_id"] == r.list_for_tab("2")[0]["media_id"]


def test_record_ignores_items_without_url_and_mse_has_no_url_requirement():
    r = MediaRegistry()
    added = r.record("7", "p", "", [{"kind": "hls"},
                                    {"url": "  ", "kind": "hls"},
                                    {"url": "blob:https://x/abc", "kind": "mse", "is_mse": True}])
    assert added == 1
    items = r.list_for_tab("7")
    assert items[0]["is_mse"] is True
    assert items[0]["url"] == "blob:https://x/abc"
