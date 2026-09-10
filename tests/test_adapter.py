# -*- coding: utf-8 -*-
"""V2 适配器回归：自调用取数（MockTransport）与协议字段映射。"""

import httpx
import pytest

from app.plugins.btdeckbridge.moviepilot_adapter import (
    MoviePilotAdapterError,
    MoviePilotV2Adapter,
)


def _history_api_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/v1/history/transfer"
    params = dict(request.url.params)
    page = int(params.get("page", "1"))
    count = int(params.get("count", "30"))
    assert params.get("apikey") == "mp-token"
    items = [
        {"id": page * count - i, "title": f"标题{i}"} for i in range(min(count, 3))
    ]
    return httpx.Response(
        200,
        json={"success": True, "data": {"list": items, "total": 7}},
    )


def _make(transport=None):
    return MoviePilotV2Adapter(
        base_url="http://127.0.0.1:3001",
        api_token="mp-token",
        transport=transport,
    )


class TestFetchPage:
    def test_fetch_page_returns_total_and_items(self):
        adapter = _make(httpx.MockTransport(_history_api_handler))
        try:
            total, items = adapter.fetch_page(1, 3)
        finally:
            adapter.close()
        assert total == 7
        assert len(items) == 3
        assert items[0]["id"] == 3

    def test_http_error_raises_user_message(self):
        adapter = _make(httpx.MockTransport(lambda request: httpx.Response(401, json={"success": False})))
        try:
            with pytest.raises(MoviePilotAdapterError, match="HTTP 401"):
                adapter.fetch_page(1, 30)
        finally:
            adapter.close()

    def test_success_false_raises(self):
        adapter = _make(httpx.MockTransport(lambda request: httpx.Response(200, json={"success": False, "message": "令牌无效"})))
        try:
            with pytest.raises(MoviePilotAdapterError, match="令牌无效"):
                adapter.fetch_page(1, 30)
        finally:
            adapter.close()

    def test_missing_token_rejected_early(self):
        adapter = MoviePilotV2Adapter(base_url="http://127.0.0.1:3001", api_token="")
        with pytest.raises(MoviePilotAdapterError, match="API_TOKEN"):
            adapter.fetch_page(1, 30)


class TestProtocolMapping:
    def test_v2_fields_map_to_protocol_item(self):
        raw = {
            "id": 42,
            "src": "/data/downloads/Movie/movie.mkv",
            "src_storage": "local",
            "src_fileitem": {"storage": "local", "path": "/data/downloads/Movie/movie.mkv", "name": "movie.mkv", "type": "file"},
            "dest": "/data/media/电影/Movie (2026)/Movie.mkv",
            "dest_storage": "local",
            "dest_fileitem": {"storage": "local", "path": "/data/media/电影/Movie (2026)/Movie.mkv", "type": "file"},
            "mode": "link",
            "type": "电影",
            "title": "Movie",
            "year": "2026",
            "seasons": None,
            "episodes": None,
            "tmdbid": 123,
            "doubanid": None,
            "media_source": "themoviedb",
            "media_id": "movie:123",
            "downloader": "qb-main",
            "download_hash": "a" * 40,
            "status": True,
            "errmsg": None,
            "date": "2026-09-01 10:00:00",
            "files": [{"path": "/data/media/电影/Movie (2026)/Movie.mkv"}],
        }
        item = MoviePilotV2Adapter.to_protocol_item(raw)
        assert item["historyId"] == 42
        assert item["srcPath"] == "/data/downloads/Movie/movie.mkv"
        assert item["srcStorage"] == "local"
        assert item["srcFileitem"]["name"] == "movie.mkv"
        assert item["destPath"] == "/data/media/电影/Movie (2026)/Movie.mkv"
        assert item["transferMode"] == "link"
        assert item["mediaType"] == "电影"
        assert item["title"] == "Movie"
        assert item["tmdbId"] == 123
        assert item["mediaSource"] == "themoviedb"
        assert item["mpDownloader"] == "qb-main"
        assert item["downloadHash"] == "a" * 40
        assert item["status"] is True
        assert item["recordedAt"] == "2026-09-01 10:00:00"
        assert item["files"][0]["path"].endswith("Movie.mkv")

    def test_non_dict_fields_coerced_to_none(self):
        item = MoviePilotV2Adapter.to_protocol_item({"id": 1, "files": "corrupt", "src_fileitem": [1, 2]})
        assert item["files"] is None
        assert item["srcFileitem"] is None
