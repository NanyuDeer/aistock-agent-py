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

    def iter_content(self, chunk_size=8192):
        yield b"fake-video-bytes"


def test_parse_share_url_extracts_video_info():
    def fake_get(url, headers=None, **kwargs):
        return _FakeResp(text=SAMPLE_HTML)

    with patch.object(dc.requests, "get", side_effect=fake_get):
        info = dc.DouyinClient().parse_share_url("https://v.douyin.com/abc123/")

    assert info["video_id"] == "123456"
    assert info["title"] == "测试视频"
    assert "play" in info["url"] and "playwm" not in info["url"]


def test_run_doctor_reports_missing_api_key():
    # 确定性：清空环境 + mock settings_obj（douyin_api_key 为空）→ key 缺失可复现，
    # 不依赖宿主 .env.development 是否配置 DOUYIN_API_KEY（Fix Task 5）。
    completed = type("Completed", (), {"returncode": 0})()
    fake_settings = type("S", (), {"douyin_api_key": ""})()

    with (
        patch.dict(dc.os.environ, {}, clear=True),
        patch.object(dc, "_load_ffmpeg", return_value=object()),
        patch.object(dc.subprocess, "run", return_value=completed),
    ):
        result = dc.run_doctor(api_key="", settings_obj=fake_settings)

    assert result["ok"] is False
    names = [c["name"] for c in result["checks"]]
    assert names == ["API_KEY", "ffmpeg-python", "FFmpeg", "FFprobe"]
    assert any(c["name"] == "API_KEY" and not c["ok"] for c in result["checks"])


class _FakeFfmpeg:
    """链式假 ffmpeg：input().output().run() 直接返回，不执行真实转换。"""

    def input(self, *args, **kwargs):
        return self

    def output(self, *args, **kwargs):
        return self

    def run(self, *args, **kwargs):
        return None


def test_extract_text_save_video_copies_video_before_cleanup(tmp_path):
    def fake_get(url, headers=None, **kwargs):
        if "aweme.snssdk.com" in url:
            return _FakeResp(text=SAMPLE_HTML, url=url)
        return _FakeResp(text=SAMPLE_HTML)

    client = dc.DouyinClient(api_key="test-key")
    with (
        patch.object(dc.requests, "get", side_effect=fake_get),
        patch.object(dc, "_load_ffmpeg", return_value=_FakeFfmpeg()),
        patch.object(client, "transcribe_audio", return_value="测试文案"),
    ):
        result = client.extract_text(
            "https://v.douyin.com/abc123/", tmp_path, save_video=True
        )

    assert result["output_path"] == str(tmp_path / "123456")
    assert (tmp_path / "123456" / "123456.mp4").exists()
    assert (tmp_path / "123456" / "transcript.md").exists()
    # 临时下载目录中的 mp4/mp3 应已清理，仅保留产物目录副本
    assert not list(client.temp_dir.glob("*.mp4"))
    assert not list(client.temp_dir.glob("*.mp3"))
