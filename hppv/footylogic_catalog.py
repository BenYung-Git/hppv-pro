"""Discover FootyLogic competitions across browse categories (league/cup/other)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from hppv.footylogic_client import (
    FOOTYLOGIC_CHANNEL_ID,
    FOOTYLOGIC_LANGUAGE_ID,
    FOOTYLOGIC_TZ_OFFSET_HKT_MIN,
    FootylogicClient,
    FootylogicClientError,
)

# Browse tabs on FootyLogic tournament/competitions.
CATEGORY_LEAGUE = 1
CATEGORY_CUP = 2
CATEGORY_INTERNATIONAL = 3
CATEGORY_OTHER = 4

CATEGORY_TYPE_MAP: dict[int, str] = {
    CATEGORY_LEAGUE: "league",
    CATEGORY_CUP: "cup",
    CATEGORY_INTERNATIONAL: "other",
    CATEGORY_OTHER: "other",
}

ALL_CATEGORY_IDS: tuple[int, ...] = (
    CATEGORY_LEAGUE,
    CATEGORY_CUP,
    CATEGORY_INTERNATIONAL,
    CATEGORY_OTHER,
)


@dataclass(frozen=True, slots=True)
class DiscoveredCompetition:
    competition_id: str
    competition_name: str
    browse_category_id: int
    browse_type: str  # league | cup | other
    group_id: str
    group_name: str
    kind: str  # nested | empty_group


@dataclass
class CategoryDiscoveryResult:
    category_id: int
    browse_type: str
    ok: bool
    error: str | None = None
    groups: int = 0
    nested_count: int = 0
    empty_group_count: int = 0
    competitions: list[DiscoveredCompetition] = field(default_factory=list)
    empty_directory: bool = False


@dataclass
class CatalogDiscoveryResult:
    ok: bool
    competitions: list[DiscoveredCompetition]
    by_category: dict[int, CategoryDiscoveryResult]
    errors: list[str]


def _competitions_params(category_id: int) -> dict[str, Any]:
    return {
        "languageId": FOOTYLOGIC_LANGUAGE_ID,
        "channelId": FOOTYLOGIC_CHANNEL_ID,
        "categoryId": int(category_id),
    }


def _seasons_params(competition_id: str) -> dict[str, Any]:
    return {
        "languageId": FOOTYLOGIC_LANGUAGE_ID,
        "channelId": FOOTYLOGIC_CHANNEL_ID,
        "competitionId": competition_id,
        "tzOffSet": FOOTYLOGIC_TZ_OFFSET_HKT_MIN,
    }


def parse_competitions_payload(
    body: dict[str, Any],
    *,
    category_id: int,
) -> CategoryDiscoveryResult:
    """Parse tournament/competitions data for one browse category."""
    browse_type = CATEGORY_TYPE_MAP.get(int(category_id), "other")
    data = body.get("data")
    if data is None:
        return CategoryDiscoveryResult(
            category_id=category_id,
            browse_type=browse_type,
            ok=False,
            error="missing_data",
        )
    if not isinstance(data, list):
        return CategoryDiscoveryResult(
            category_id=category_id,
            browse_type=browse_type,
            ok=False,
            error="data_not_list",
        )
    if len(data) == 0:
        return CategoryDiscoveryResult(
            category_id=category_id,
            browse_type=browse_type,
            ok=True,
            groups=0,
            empty_directory=True,
        )

    found: list[DiscoveredCompetition] = []
    nested = 0
    empty_groups = 0
    for group in data:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("categoryId") or "").strip()
        group_name = str(group.get("categoryName") or "").strip()
        comps = group.get("competitions")
        if isinstance(comps, list) and len(comps) > 0:
            for c in comps:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("competitionId") or "").strip()
                cname = str(c.get("competitionName") or "").strip()
                if not cid:
                    continue
                nested += 1
                found.append(
                    DiscoveredCompetition(
                        competition_id=cid,
                        competition_name=cname or cid,
                        browse_category_id=int(category_id),
                        browse_type=browse_type,
                        group_id=group_id or cid,
                        group_name=group_name or cname or cid,
                        kind="nested",
                    )
                )
        else:
            # Empty nested list: treat group categoryId as competitionId (cups/other).
            if not group_id:
                continue
            empty_groups += 1
            found.append(
                DiscoveredCompetition(
                    competition_id=group_id,
                    competition_name=group_name or group_id,
                    browse_category_id=int(category_id),
                    browse_type=browse_type,
                    group_id=group_id,
                    group_name=group_name or group_id,
                    kind="empty_group",
                )
            )

    return CategoryDiscoveryResult(
        category_id=category_id,
        browse_type=browse_type,
        ok=True,
        groups=len(data),
        nested_count=nested,
        empty_group_count=empty_groups,
        competitions=found,
        empty_directory=len(found) == 0,
    )


def _dedupe_competitions(
    items: list[DiscoveredCompetition],
) -> list[DiscoveredCompetition]:
    """Keep first occurrence (league → cup → international → other order)."""
    seen: set[str] = set()
    out: list[DiscoveredCompetition] = []
    for item in items:
        if item.competition_id in seen:
            continue
        seen.add(item.competition_id)
        out.append(item)
    return out


def discover_competitions(
    client: FootylogicClient,
    *,
    category_ids: tuple[int, ...] = ALL_CATEGORY_IDS,
) -> CatalogDiscoveryResult:
    """Fetch and flatten competitions for all requested browse categories."""
    by_cat: dict[int, CategoryDiscoveryResult] = {}
    errors: list[str] = []
    collected: list[DiscoveredCompetition] = []

    for cat_id in category_ids:
        browse_type = CATEGORY_TYPE_MAP.get(int(cat_id), "other")
        try:
            result = client.get_json(
                "/tournament/competitions", _competitions_params(cat_id)
            )
        except FootylogicClientError as e:
            msg = f"category_{cat_id}_request_failed:{e.code}:{e.detail}"
            errors.append(msg)
            by_cat[cat_id] = CategoryDiscoveryResult(
                category_id=cat_id,
                browse_type=browse_type,
                ok=False,
                error=msg,
            )
            continue

        if not result.business_ok or result.body is None:
            msg = (
                f"category_{cat_id}_business_error:"
                f"{result.message or result.http_status}"
            )
            errors.append(msg)
            by_cat[cat_id] = CategoryDiscoveryResult(
                category_id=cat_id,
                browse_type=browse_type,
                ok=False,
                error=msg,
            )
            continue

        parsed = parse_competitions_payload(result.body, category_id=cat_id)
        by_cat[cat_id] = parsed
        if not parsed.ok:
            errors.append(f"category_{cat_id}_parse:{parsed.error}")
            continue
        collected.extend(parsed.competitions)

    deduped = _dedupe_competitions(collected)
    # Success requires every requested category fetched OK (empty OK is fine).
    ok = all(by_cat.get(cid) and by_cat[cid].ok for cid in category_ids) and not errors
    return CatalogDiscoveryResult(
        ok=ok,
        competitions=deduped,
        by_category=by_cat,
        errors=errors,
    )


def fetch_fixture_seasons(
    client: FootylogicClient,
    competition_id: str,
) -> tuple[bool, list[dict[str, Any]], str | None]:
    """
    Return (ok, seasons, error).
    Each season dict: seasonId, seasonName, fixtureRange (list[str YYYY-MM]).
    """
    try:
        result = client.get_json(
            "/tournament/fixtures-seasons", _seasons_params(competition_id)
        )
    except FootylogicClientError as e:
        return False, [], f"{e.code}:{e.detail}"

    if not result.business_ok or result.body is None:
        return False, [], f"business_error:{result.message or result.http_status}"

    data = result.body.get("data")
    if data is None:
        # Distinguish empty vs missing: null/missing → uncertain failure.
        return False, [], "missing_seasons_data"
    if not isinstance(data, list):
        return False, [], "seasons_data_not_list"

    seasons: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sid = row.get("seasonId")
        if sid is None or str(sid).strip() == "":
            continue
        fr = row.get("fixtureRange") or []
        months = [str(m).strip() for m in fr if str(m).strip()] if isinstance(fr, list) else []
        seasons.append(
            {
                "seasonId": str(sid).strip(),
                "seasonName": str(row.get("seasonName") or "").strip(),
                "fixtureRange": months,
            }
        )
    return True, seasons, None


def months_for_year(fixture_range: list[str], year: int) -> list[str]:
    """Months in fixtureRange that belong to the calendar year."""
    prefix = f"{int(year):04d}-"
    return [m for m in fixture_range if isinstance(m, str) and m.startswith(prefix)]


GetJsonFn = Callable[[str, dict[str, Any]], Any]
