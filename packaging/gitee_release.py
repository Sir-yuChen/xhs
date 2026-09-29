"""Gitee Release 创建 + 附件上传（幂等，可重复执行）。

XHS_ALL_IN_ONE 正式版发布工作流的**发布环节实现**（设计 14 §8），由 `packaging/release.ps1`
在第 6 阶段调用；也可单独执行。

令牌来源：Windows 凭据管理器（`git credential fill`）—— 只在内存流转，不落盘、不回显。

用法：
    python gitee_release.py list
    python gitee_release.py publish --tag v1.0.1 \
        --exe D:\\work\\XHS\\spider-xhs-setup-1.0.1.exe \
        --name "v1.0.1 · 正式版" --body-file release-note.md --replace
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_TMPL = "https://gitee.com/api/v5/repos/%s/%s"


def get_credential(repo_dir):
    """从 git credential manager 取 gitee 凭据；不打印明文。"""
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=gitee.com\n\n",
        capture_output=True,
        text=True,
        cwd=repo_dir,
        timeout=60,
    )
    username, password = None, None
    for line in proc.stdout.splitlines():
        if line.startswith("username="):
            username = line.split("=", 1)[1]
        elif line.startswith("password="):
            password = line.split("=", 1)[1]
    if not password:
        raise SystemExit(
            "未从凭据管理器取到 gitee 凭据。请提供 Gitee 私人令牌（设置 -> 私人令牌，勾 projects 权限）。"
        )
    return username, password


def _sanitize(text, token):
    return text.replace(token, "<TOKEN>") if token else text


def api_get(url, token, timeout=60):
    req = urllib.request.Request(url + "?access_token=" + urllib.parse.quote(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit("GET 失败 %s: %s" % (exc.code, _sanitize(detail, token)[:500]))


def api_post_form(url, fields, token, timeout=120, method="POST"):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url + "?access_token=" + urllib.parse.quote(token), data=data, method=method
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit("POST 失败 %s: %s" % (exc.code, _sanitize(detail, token)[:800]))


def api_delete(url, token, timeout=120):
    req = urllib.request.Request(
        url + "?access_token=" + urllib.parse.quote(token), method="DELETE"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit("DELETE 失败 %s: %s" % (exc.code, _sanitize(detail, token)[:500]))


def find_attach_id(api, release_id, filename, token):
    """附件 id 只在 /attach_files 接口里返回（release.assets 不含 id）。"""
    items = api_get("%s/releases/%s/attach_files" % (api, release_id), token)
    for item in items or []:
        if item.get("name") == filename:
            return item.get("id")
    return None


def upload_attachment(api, release_id, path, token):
    """手写 multipart/form-data（urllib 无内置）。"""
    boundary = "----ReleaseBoundary" + uuid.uuid4().hex
    filename = os.path.basename(path)
    with open(path, "rb") as fh:
        blob = fh.read()

    chunks = [
        ("--%s\r\n" % boundary).encode(),
        b'Content-Disposition: form-data; name="access_token"\r\n\r\n',
        token.encode() + b"\r\n",
        ("--%s\r\n" % boundary).encode(),
        ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % filename).encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        blob,
        ("\r\n--%s--\r\n" % boundary).encode(),
    ]
    body = b"".join(chunks)

    url = "%s/releases/%s/attach_files" % (api, release_id)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit("上传失败 %s: %s" % (exc.code, _sanitize(detail, token)[:800]))


def main():
    ap = argparse.ArgumentParser(description="Gitee Release 发布")
    ap.add_argument("action", choices=["list", "publish"])
    ap.add_argument("--owner", default="Sir-yuChen")
    ap.add_argument("--repo", default="xhs")
    ap.add_argument("--tag")
    ap.add_argument("--exe", help="待上传附件（安装包 / 压缩包）绝对路径")
    ap.add_argument("--name", default=None, help="Release 标题，默认与 tag 相同")
    ap.add_argument("--body-file", default=None, help="Release 说明 markdown 文件")
    ap.add_argument("--body", default=None, help="Release 说明（与 --body-file 二选一）")
    ap.add_argument("--target", default=None, help="target_commitish，默认不传")
    ap.add_argument("--prerelease", action="store_true")
    ap.add_argument("--replace", action="store_true",
                    help="同名附件已存在时：先删旧附件再传新的（发版重打用）")
    ap.add_argument("--repo-dir", default=os.getcwd(), help="用于 git credential fill 的仓库目录")
    args = ap.parse_args()

    api = API_TMPL % (args.owner, args.repo)
    username, token = get_credential(args.repo_dir)
    print("凭据用户: %s（令牌已获取，长度 %d，不显示）" % (username, len(token)))

    releases = api_get(api + "/releases", token)
    print("当前 release 数: %d" % len(releases))

    if args.action == "list":
        for rel in releases:
            print("  - id=%s tag=%s name=%s assets=%s"
                  % (rel["id"], rel.get("tag_name"), rel.get("name"), len(rel.get("assets") or [])))
        return

    if not args.tag:
        raise SystemExit("publish 需要 --tag")
    if not args.exe:
        raise SystemExit("publish 需要 --exe")
    if not os.path.exists(args.exe):
        raise SystemExit("附件不存在: %s" % args.exe)

    size = os.path.getsize(args.exe)
    print("待上传: %s (%d B / %.1f MB)" % (os.path.basename(args.exe), size, size / 1024 / 1024))

    body = args.body or ""
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as fh:
            body = fh.read()

    existing = [r for r in releases if r.get("tag_name") == args.tag]
    if existing:
        release = existing[0]
        print("复用已有 release id=%s" % release["id"])
        if body:
            api_post_form(
                "%s/releases/%s" % (api, release["id"]),
                {
                    "tag_name": args.tag,
                    "body": body,
                    "name": args.name or release.get("name") or args.tag,
                },
                token,
                method="PATCH",
            )
            print("已更新 release 说明")
    else:
        fields = {
            "tag_name": args.tag,
            "name": args.name or args.tag,
            "body": body,
            "prerelease": "true" if args.prerelease else "false",
        }
        if args.target:
            fields["target_commitish"] = args.target
        release = api_post_form(api + "/releases", fields, token)
        print("已创建 release id=%s" % release["id"])

    target = os.path.basename(args.exe)
    old_asset = next(
        (a for a in (release.get("assets") or []) if a.get("name") == target), None
    )
    if old_asset and not args.replace:
        print("附件已存在，跳过上传: %s（要覆盖请加 --replace）" % target)
    else:
        if old_asset:
            attach_id = find_attach_id(api, release["id"], target, token)
            if not attach_id:
                raise SystemExit("同名附件存在但取不到附件 id，无法删除：%s" % old_asset)
            api_delete("%s/releases/%s/attach_files/%s" % (api, release["id"], attach_id), token)
            print("已删除旧附件: %s (id=%s)" % (target, attach_id))
        info = upload_attachment(api, release["id"], args.exe, token)
        print("附件上传成功: name=%s size=%s" % (info.get("name"), info.get("size")))

    final = api_get("%s/releases/tags/%s" % (api, args.tag), token)
    print("最终 release: id=%s tag=%s name=%s"
          % (final["id"], final.get("tag_name"), final.get("name")))
    for asset in final.get("assets") or []:
        print("  asset: %s -> %s"
              % (asset.get("name"), asset.get("browser_download_url")))
    print("RELEASE_PAGE=https://gitee.com/%s/%s/releases/tag/%s" % (args.owner, args.repo, args.tag))


if __name__ == "__main__":
    main()
