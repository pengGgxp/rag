"""Retain the newest SemVer images and their SHA aliases in Huawei Cloud SWR.

Only recognizable release tags are eligible for deletion. The current and
previous release digests are always protected, including every tag that points
to either digest. A failed or incomplete inventory aborts before deletion.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
SHA_TAG_RE = re.compile(r"^sha-[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORIES = ("zhiku-api", "zhiku-worker", "zhiku-web")
PAGE_SIZE = 1000


@dataclass(frozen=True)
class Tag:
    name: str
    digest: str


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--protected-file", required=True)
    parser.add_argument("--region")
    parser.add_argument("--endpoint")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def protected_digests(path: str | None) -> set[str]:
    if not path:
        raise ValueError("a protected digest file is required")
    values: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            value = line.strip()
            if DIGEST_RE.fullmatch(value):
                values.add(value)
            elif value:
                raise ValueError("protected digest file contains an invalid digest")
    return values


def version_key(value: str) -> tuple[int, int, int]:
    if not VERSION_RE.fullmatch(value):
        raise ValueError(f"invalid release tag: {value}")
    parts = tuple(int(part) for part in value[1:].split("."))
    return parts


def select_tags_to_delete(
    tags: Iterable[Tag],
    keep: int,
    protected: set[str],
) -> tuple[list[str], list[str]]:
    """Return (tags to delete, unknown tags retained) without risking rollback."""
    if keep < 2:
        raise ValueError("at least two release versions must be retained")
    if not protected:
        raise ValueError("at least one protected digest is required")

    by_name: dict[str, Tag] = {}
    duplicate_names: set[str] = set()
    for tag in tags:
        if not tag.name or not DIGEST_RE.fullmatch(tag.digest):
            continue
        if tag.name in by_name:
            duplicate_names.add(tag.name)
        by_name[tag.name] = tag
    if duplicate_names:
        raise ValueError("registry inventory contains duplicate tag names")

    releases = sorted(
        (
            tag
            for tag in by_name.values()
            if VERSION_RE.fullmatch(tag.name) and tag.digest in protected
        ),
        key=lambda tag: version_key(tag.name),
        reverse=True,
    )
    all_releases = sorted(
        (tag for tag in by_name.values() if VERSION_RE.fullmatch(tag.name)),
        key=lambda tag: version_key(tag.name),
        reverse=True,
    )
    retained_versions = {tag.name for tag in all_releases[:keep]}
    retained_versions.update(tag.name for tag in releases)
    retained_digests = protected | {
        tag.digest for tag in by_name.values() if tag.name in retained_versions
    }

    delete: set[str] = set()
    unknown: set[str] = set()
    for tag in by_name.values():
        if tag.digest in protected:
            continue
        if VERSION_RE.fullmatch(tag.name):
            if tag.name not in retained_versions:
                delete.add(tag.name)
            continue
        if SHA_TAG_RE.fullmatch(tag.name):
            if tag.digest not in retained_digests:
                delete.add(tag.name)
            continue
        unknown.add(tag.name)
    return sorted(delete), sorted(unknown)


def _client(args: argparse.Namespace):
    from huaweicloudsdkcore.auth.credentials import BasicCredentials
    from huaweicloudsdkswr.v2 import SwrClient

    access_key = os.environ.get("SWR_ACCESS_KEY", "")
    secret_key = os.environ.get("SWR_SECRET_KEY", "")
    project_id = os.environ.get("SWR_PROJECT_ID", "")
    region = args.region or os.environ.get("SWR_REGION", "")
    registry = urlparse(args.registry)
    if (
        registry.scheme not in {"", "https"}
        or not registry.netloc
        or not registry.hostname
        or registry.username
        or registry.password
        or registry.path not in {"", "/"}
        or registry.query
        or registry.fragment
    ):
        raise ValueError("SWR registry must be an HTTPS registry hostname")
    if not all((access_key, secret_key, project_id, region)):
        raise ValueError(
            "SWR_ACCESS_KEY, SWR_SECRET_KEY, SWR_PROJECT_ID and SWR_REGION are required"
        )
    endpoint = args.endpoint or f"https://swr.{region}.myhuaweicloud.com"
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("SWR endpoint must be an HTTPS origin without credentials")
    credentials = BasicCredentials(access_key, secret_key, project_id)
    return (
        SwrClient.new_builder()
        .with_credentials(credentials)
        .with_endpoint(endpoint.rstrip("/"))
        .build()
    )


def list_tags(client, organization: str, repository: str) -> list[Tag]:
    from huaweicloudsdkswr.v2 import ListRepositoryTagsRequest

    tags: list[Tag] = []
    offset = 0
    while True:
        response = client.list_repository_tags(
            ListRepositoryTagsRequest(
                namespace=organization,
                repository=repository,
                limit=str(PAGE_SIZE),
                offset=str(offset),
                order_column="updated_at",
                order_type="desc",
            )
        )
        body = getattr(response, "body", None) or []
        for item in body:
            name = getattr(item, "tag", None)
            digest = getattr(item, "digest", None)
            if name and digest:
                tags.append(Tag(name=name, digest=digest))
        if len(body) < PAGE_SIZE:
            return tags
        offset += PAGE_SIZE


def delete_tag(client, organization: str, repository: str, tag: str) -> None:
    from huaweicloudsdkswr.v2 import DeleteRepoTagRequest

    client.delete_repo_tag(
        DeleteRepoTagRequest(
            namespace=organization,
            repository=repository,
            tag=tag,
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        protected = protected_digests(args.protected_file)
        if not protected:
            raise ValueError("protected digest file is empty")
        if args.keep < 2:
            raise ValueError("at least two release versions must be retained")
        enabled = os.environ.get("SWR_DELETE_ENABLED", "").lower() in {
            "1",
            "true",
            "yes",
        }
        if args.dry_run or not enabled:
            print("SWR deletion is disabled; validating inventory only.")
        client = None if args.dry_run or not enabled else _client(args)
        for repository in REPOSITORIES:
            if client is None:
                print(f"{repository}: deletion disabled")
                continue
            tags = list_tags(client, args.organization, repository)
            delete, unknown = select_tags_to_delete(tags, args.keep, protected)
            if unknown:
                print(
                    f"{repository}: retaining {len(unknown)} unrecognized tag(s): "
                    + ", ".join(unknown[:10])
                )
            if args.dry_run:
                print(
                    f"{repository}: would delete {len(delete)} tag(s): "
                    + (", ".join(delete) if delete else "none")
                )
                continue
            for name in delete:
                delete_tag(client, args.organization, repository, name)
                print(f"{repository}: deleted {name}")
            print(f"{repository}: retained {args.keep} release version(s)")
    except (ImportError, OSError, ValueError) as exc:
        print(f"SWR cleanup refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
