"""Assign missing SD Farm UIDs to FREE ixBrowser profiles and move to AQIBPC1."""
from __future__ import annotations

from ixbrowser_local_api import IXBrowserClient
from ixbrowser_local_api.entities import Profile

MISSING_UIDS = [
    "61588169341768",
    "61574310346065",
    "61573225565576",
    "61572076380213",
    "61572370505339",
    "61572156453203",
    "61572138632853",
    "61574312717849",
    "61569750060983",
    "61570705743520",
    "61572109543921",
    "61572160696052",
]
TARGET_GROUP_ID = 286548  # AQIBPC1


def fetch_all_profiles(client: IXBrowserClient) -> list[dict]:
    profiles: list[dict] = []
    page = 1
    while True:
        data = client.get_profile_list(page=page, limit=100)
        if data is None:
            raise RuntimeError(f"profile-list failed page={page} code={client.code} msg={client.message}")
        if not data:
            break
        profiles.extend(data)
        if len(data) < 100:
            break
        page += 1
    return profiles


def main() -> None:
    client = IXBrowserClient()
    profiles = fetch_all_profiles(client)
    free_profiles = [p for p in profiles if str(p.get("name", "")).strip() == "FREE"]
    free_profiles.sort(key=lambda p: int(p["profile_id"]))
    print(f"available FREE profiles: {len(free_profiles)}")
    if len(free_profiles) < len(MISSING_UIDS):
        raise SystemExit(f"Need {len(MISSING_UIDS)} FREE profiles, only {len(free_profiles)} available")

    picked = free_profiles[: len(MISSING_UIDS)]
    renamed_ids: list[int] = []
    for uid, src in zip(MISSING_UIDS, picked):
        pid = int(src["profile_id"])
        prof = Profile({"profile_id": pid, "name": uid, "group_id": TARGET_GROUP_ID})
        result = client.update_profile(prof)
        if result is None:
            raise SystemExit(f"rename failed profile_id={pid} uid={uid} code={client.code} msg={client.message}")
        renamed_ids.append(pid)
        print(f"RENAMED profile_id={pid} FREE -> {uid} group={TARGET_GROUP_ID}")

    refreshed = fetch_all_profiles(client)
    by_id = {int(p["profile_id"]): p for p in refreshed}
    for uid, pid in zip(MISSING_UIDS, renamed_ids):
        p = by_id[pid]
        ok = str(p.get("name")) == uid and int(p.get("group_id")) == TARGET_GROUP_ID
        print(
            f"VERIFY {uid} profile_id={pid} name={p.get('name')!r} "
            f"group={p.get('group_id')} group_name={p.get('group_name')!r} ok={ok}"
        )


if __name__ == "__main__":
    main()
