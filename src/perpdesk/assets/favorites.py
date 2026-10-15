"""Favourites: seed defaults, add/remove, reorder, group."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from perpdesk.assets.registry import CLASSES, create_asset
from perpdesk.config import Settings
from perpdesk.db.models import Asset, Favorite, FavoriteGroup
from perpdesk.sources.hyperliquid import Hyperliquid
from perpdesk.store import kv_get, kv_set


def list_favorites(s: Session) -> list[Favorite]:
    q = (select(Favorite).outerjoin(FavoriteGroup, Favorite.group_id == FavoriteGroup.id)
         .order_by(func.coalesce(FavoriteGroup.position, 999), Favorite.position, Favorite.id))
    return list(s.execute(q).scalars().unique())


def groups(s: Session) -> list[FavoriteGroup]:
    return list(s.execute(select(FavoriteGroup).order_by(FavoriteGroup.position)).scalars())


def get_or_create_group(s: Session, name: str) -> FavoriteGroup:
    g = s.execute(select(FavoriteGroup).where(FavoriteGroup.name == name)).scalar_one_or_none()
    if g is None:
        pos = (s.execute(select(func.max(FavoriteGroup.position))).scalar() or 0) + 1
        g = FavoriteGroup(name=name, position=pos)
        s.add(g)
        s.flush()
    return g


async def seed_defaults(s: Session, settings: Settings, hl: Hyperliquid) -> list[str]:
    """Create the default favourites once. Returns symbols that could not be resolved."""
    if kv_get(s, "favorites_seeded"):
        return []
    from perpdesk.assets.registry import universe

    try:
        await universe(hl)
    except Exception as e:
        return [f"seeding postponed, Hyperliquid unavailable: {e}"]
    missing = []
    for gi, (gname, syms) in enumerate(settings.favorite_groups):
        g = get_or_create_group(s, gname)
        g.position = gi
        for pos, sym in enumerate(syms):
            try:
                a = await create_asset(s, sym, hl, gname)
            except Exception:
                missing.append(sym)
                continue
            if s.execute(select(Favorite).where(Favorite.asset_id == a.id)).scalar_one_or_none() is None:
                s.add(Favorite(asset_id=a.id, group_id=g.id, position=pos))
    kv_set(s, "favorites_seeded", True)
    return missing


async def add_favorite(s: Session, key: str, hl: Hyperliquid, group: str | None = None) -> Asset:
    a = await create_asset(s, key, hl, group)
    fav = s.execute(select(Favorite).where(Favorite.asset_id == a.id)).scalar_one_or_none()
    if fav is None:
        g = get_or_create_group(s, group) if group else None
        pos = (s.execute(select(func.max(Favorite.position))).scalar() or 0) + 1
        s.add(Favorite(asset_id=a.id, group_id=g.id if g else None, position=pos))
    return a


def remove_favorite(s: Session, asset_id: int) -> None:
    fav = s.execute(select(Favorite).where(Favorite.asset_id == asset_id)).scalar_one_or_none()
    if fav:
        s.delete(fav)


def reorder(s: Session, asset_ids: list[int], group_name: str | None = None) -> None:
    g = get_or_create_group(s, group_name) if group_name else None
    for pos, aid in enumerate(asset_ids):
        fav = s.execute(select(Favorite).where(Favorite.asset_id == aid)).scalar_one_or_none()
        if fav:
            fav.position = pos
            if g is not None:
                fav.group_id = g.id
                if g.name in CLASSES:
                    fav.asset.asset_class = g.name


def move_to_group(s: Session, asset_id: int, group_name: str) -> None:
    g = get_or_create_group(s, group_name)
    fav = s.execute(select(Favorite).where(Favorite.asset_id == asset_id)).scalar_one_or_none()
    if fav:
        fav.group_id = g.id
        if g.name in CLASSES:
            fav.asset.asset_class = g.name
