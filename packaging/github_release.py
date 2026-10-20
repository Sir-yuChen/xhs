"""GitHub Release 创建 + 附件上传（**主发布通道**）。

与 `gitee_release.py` 接口一致（`list` / `publish`），由 `release.ps1` 第 7 阶段调用。

为什么要另开一路：Gitee 对**单个附件**有 100 MB 硬上限
（`验证失败：文件大小已超出限制：100 MB`），而安装包 1.0.4 已到 113.6 MB
⇒ Gitee 放不下，改以 GitHub 为主、Gitee 为辅（Gitee 能传就传，传不上不阻断发版）。

令牌来源：Windows 凭据管理器（`git credential fill` for github.com）——
只在内存流转，不落盘、不回显。

⚠️ 本机 `api.github.com` 直连**不稳定**（常被 TLS 层掐断，表现为
`000` / `IncompleteRead`）⇒ 每个请求都带**自动重试 + 退避**，不能只试一次。

用法：
    python github_release.py list
    python github_release.py publish --tag v1.0.4 \
        --exe D:\\work\\XHS\\spider-xhs-setup-1.0.4.exe \
        --name "v1.0.4 · 正式版" --body-file release-note.md --replace
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


def _sanitize(text, token):
    """日志里绝不出现令牌。"""
    if token:
        text = text.replace(token, "[REDACTED]")
    return text


def get_token(repo_dir):
    """从 git credential manager 取 github.com 的令牌；不打印明文。"""
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        cwd=repo_dir,
        timeout=60,
    )
    token = None
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            token = line.split("=", 1)[1]
    if not token:
        raise SystemExit(
            "未取到 GitHub 令牌（git credential fill 无 password）——"
            "请先在 Windows 凭据管理器里配置 github.com 的 PAT"
        )
    print("令牌已获取（长度 %d，不显示）" % len(token))
    return token


def request(url, token, *, method="GET", body=None, timeout=60, tries=8,
            content_type="application/json", raw=None):
    """带重试的 GitHub API 请求。

    本机直连 api.github.com 会间歇性被掐断（`IncompleteRead` / 超时 / 000），
    必须重试；`raw` 用于二进制上传（不经过 JSON 序列化）。
    """
    data = raw
    if data is None and body is not None:
        data = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": "token %s" % token,
        "User-Agent": "xhs-release",
        "Accept": "application/vnd.github+json",
        "Content-Type": content_type,
    }
    last = None
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                return json.loads(payload.decode("utf-8")) if payload else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            #: 4xx 是确定性错误，重试无意义（409 例外：附件同名）
            if exc.code < 500 and exc.code != 409:
                raise SystemExit(
                    "请求失败 %s: %s" % (exc.code, _sanitize(detail, token)[:800])
                )
            last = "HTTP %s: %s" % (exc.code, _sanitize(detail, token)[:200])
        except Exception as exc:  # noqa: BLE001 —— 网络层抖动
            last = "%s: %s" % (type(exc).__name__, _sanitize(str(exc), token)[:200])
        if attempt < tries:
            wait = min(2 * attempt, 20)
            print("  第 %d 次失败（%s），%ds 后重试" % (attempt, last, wait))
            time.sleep(wait)
    raise SystemExit("请求最终失败（重试 %d 次）：%s" % (tries, last))


def find_release(owner, repo, tag, token):
    releases = request("%s/repos/%s/%s/releases?per_page=100" % (API, owner, repo), token)
    for rel in releases:
        if rel.get("tag_name") == tag:
            return rel
    return None


def upload_asset(owner, repo, release, path, token):
    """上传附件；同名先删（等价于 Gitee 侧的 --replace）。"""
    name = os.path.basename(path)
    for asset in release.get("assets") or []:
        if asset.get("name") == name:
            print("删除同名附件 id=%s" % asset.get("id"))
            request(
                "%s/repos/%s/%s/releases/assets/%s" % (API, owner, repo, asset["id"]),
                token,
                method="DELETE",
            )
    size = os.path.getsize(path)
    print("待上传: %s (%.1f MB)" % (name, size / 1048576))
    with open(path, "rb") as fh:
        blob = fh.read()
    url = "%s/repos/%s/%s/releases/%s/assets?name=%s" % (
        UPLOADS, owner, repo, release["id"], urllib.parse.quote(name)
    )
    result = request(
        url, token, method="POST", raw=blob,
        timeout=1800, tries=5, content_type="application/octet-stream",
    )
    print("附件上传成功: %s (%s B)" % (result.get("name"), result.get("size")))
    return result


def cmd_list(owner, repo, token):
    releases = request("%s/repos/%s/%s/releases?per_page=100" % (API, owner, repo), token)
    print("当前 release 数: %d" % len(releases))
    for rel in releases:
        print("  - id=%s tag=%s name=%s assets=%d" % (
            rel.get("id"), rel.get("tag_name"), rel.get("name"),
            len(rel.get("assets") or []),
        ))


def cmd_publish(args, token):
    body = args.body
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as fh:
            body = fh.read()

    existing = find_release(args.owner, args.repo, args.tag, token)
    payload = {
        "tag_name": args.tag,
        "name": args.name or args.tag,
        "body": body or "",
        "prerelease": bool(args.prerelease),
    }
    if args.target:
        payload["target_commitish"] = args.target

    if existing and args.replace:
        print("复用已有 release id=%s" % existing.get("id"))
        release = request(
            "%s/repos/%s/%s/releases/%s" % (API, args.owner, args.repo, existing["id"]),
            token, method="PATCH", body=payload,
        )
        print("已更新 release 说明")
    elif existing:
        release = existing
        print("release 已存在，未加 --replace，跳过更新")
    else:
        release = request(
            "%s/repos/%s/%s/releases" % (API, args.owner, args.repo),
            token, method="POST", body=payload,
        )
        print("已创建 release id=%s" % release.get("id"))

    if args.exe:
        upload_asset(args.owner, args.repo, release, args.exe, token)

    print("RELEASE_PAGE %s" % release.get("html_url"))
    print("下载直链 %s/repos/%s/%s/releases/download/%s/%s" % (
        API, args.owner, args.repo, args.tag, os.path.basename(args.exe or "")
    ))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "publish"])
    ap.add_argument("--owner", default="Sir-yuChen")
    ap.add_argument("--repo", default="xhs")
    ap.add_argument("--tag")
    ap.add_argument("--exe", help="待上传附件绝对路径")
    ap.add_argument("--name", default=None)
    ap.add_argument("--body-file", default=None)
    ap.add_argument("--body", default=None)
    ap.add_argument("--target", default=None)
    ap.add_argument("--prerelease", action="store_true")
    ap.add_argument("--replace", action="store_true", help="已存在则覆盖说明并替换同名附件")
    ap.add_argument("--repo-dir", default=os.getcwd())
    args = ap.parse_args()

    token = get_token(args.repo_dir)
    if args.action == "list":
        cmd_list(args.owner, args.repo, token)
    else:
        if not args.tag:
            raise SystemExit("publish 需要 --tag")
        if args.exe and not os.path.isfile(args.exe):
            raise SystemExit("附件不存在：%s" % args.exe)
        cmd_publish(args, token)


if __name__ == "__main__":
    sys.exit(main())
