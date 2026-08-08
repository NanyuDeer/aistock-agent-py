from unittest.mock import patch

from aistock_agent.skills import douyin_client as dc

SAMPLE_HTML = """
<html><body>
<script>window._ROUTER_DATA = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [{
  "desc": "测试视频 #财经",
  "video": {"play_addr": {"url_list": ["https://aweme.snssdk.com/playwm?v=1"]}}
}]}}}}</script>
</body></html>
"""


class _FakeResp:
    def __init__(self, text="", url="https://www.iesdouyin.com/share/video/123456"):
        self.text = text
        self.url = url
        self.ok = True

    def raise_for_status(self):
        pass


def test_parse_share_url_extracts_video_info():
    def fake_get(url, headers=None):
        return _FakeResp(text=SAMPLE_HTML)

    with patch.object(dc.requests, "get", side_effect=fake_get):
        info = dc.DouyinClient().parse_share_url("https://v.douyin.com/abc123/")

    assert info["video_id"] == "123456"
    assert info["title"] == "测试视频"
    assert "play" in info["url"] and "playwm" not in info["url"]


def test_run_doctor_reports_missing_api_key():
    result = dc.run_doctor(api_key="")
    assert result["ok"] is False
    names = [c["name"] for c in result["checks"]]
    assert names == ["API_KEY", "ffmpeg-python", "FFmpeg", "FFprobe"]
    assert any(c["name"] == "API_KEY" and not c["ok"] for c in result["checks"])
