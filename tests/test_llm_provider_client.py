# 用例：LLM 客户端构造（本机 endpoint 直连绕开系统/环境代理）
# 纯单元测试：不打网络，只校验客户端构造参数与缓存复用
import pytest

from ai import llm_provider as lp


@pytest.fixture(autouse=True)
def _clear_clients():
    """用例间清理全局客户端缓存，避免相互干扰"""
    lp._clients.clear()
    yield
    lp._clients.clear()


@pytest.mark.parametrize("base_url", [
    "http://localhost:11434/v1",
    "http://127.0.0.1:11434/v1",
    "http://[::1]:11434/v1",
    "http://0.0.0.0:8000/v1",
    "http://LocalHost:11434/v1",
])
def test_is_local_base_true(base_url):
    """回环地址（含大小写、IPv6、显式端口）判为本机"""
    assert lp._is_local_base(base_url) is True


@pytest.mark.parametrize("base_url", [
    "https://api.deepseek.com", "https://api.deepseek.com/v1",
    "http://192.168.1.10:11434/v1", "http://10.0.0.5/v1", "", "not a url",
])
def test_is_local_base_false(base_url):
    """远端/局域网/空串/非法串一律不走本机直连"""
    assert lp._is_local_base(base_url) is False


def test_local_endpoint_bypasses_proxy(monkeypatch):
    """本机 endpoint：底层 httpx 不吃环境代理（_mounts 为空），即便设置了代理变量"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7897")

    client = lp._get_client("http://localhost:11434/v1", "k")

    assert client._client._mounts == {}   # 无代理挂载 → 请求直连本机


def test_remote_endpoint_keeps_proxy(monkeypatch):
    """远端 endpoint：仍按环境代理构建（保留访问外网所需的代理能力）"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    client = lp._get_client("https://api.deepseek.com", "k")

    assert client._client._mounts != {}


def test_client_cached_by_base_and_key():
    """同 (base_url, api_key) 复用实例；不同 base_url 各自建实例"""
    a = lp._get_client("http://localhost:11434/v1", "k")
    assert lp._get_client("http://localhost:11434/v1", "k") is a
    assert lp._get_client("http://127.0.0.1:11434/v1", "k") is not a
    assert lp._get_client("http://localhost:11434/v1", "k2") is not a
