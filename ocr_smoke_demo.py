"""Throwaway file for OpenCodeReview action smoke test — not part of the codebase."""


def add_item(items: list | None = None) -> list:
    if items is None:
        items = []
    items.append("item")
    return items
