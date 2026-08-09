"""Full integration test — instantiate MainWindow, push article, inspect tree."""
import sys, os
sys.path.insert(0, '.')

# Set license so MainWindow.__init__ doesn't bail.
os.environ.setdefault("THRESEARCH_LICENSE", "8280e5b347cda80cfccb363e33e56693")

import tkinter as tk
from datetime import datetime, timezone
from models.article import Article

try:
    root = tk.Tk()
    root.withdraw()
    # Construct MainWindow. It needs config + callbacks.
    from main import load_config
    from utils.paths import APP_ROOT
    cfg = load_config(APP_ROOT / "config.yaml")

    from desktop.main_window import MainWindow
    mw = MainWindow(
        config=cfg,
        on_save_settings=lambda c: None,
        on_open_article=lambda a: None,
        on_config_saved=lambda c: None,
    )

    # Push 2 articles.
    a1 = Article(
        title="US soybean oil use for biofuels production hits record high",
        link="https://www.reuters.com/markets/commodities/soybean-oil-biofuels-2026-08-09",
        pub_date=datetime(2026, 8, 9, 13, 31, tzinfo=timezone.utc),
        summary="USDA report shows demand jumped 30% this quarter.",
        source="Google News",
        commodity_tags=["Soybean Oil"],
    )
    a2 = Article(
        title="Gold price climbs to monthly high on weaker dollar",
        link="https://www.bloomberg.com/news/2026-08-09/gold-monthly-high",
        pub_date=datetime(2026, 8, 9, 12, 15, tzinfo=timezone.utc),
        summary="Spot gold rose 1.5% to $2,050/oz.",
        source="Bloomberg",
        commodity_tags=["Gold"],
    )
    a_no_link = Article(
        title="Coffee futures steady in early trading",
        link="",
        pub_date=datetime(2026, 8, 9, 11, 0, tzinfo=timezone.utc),
        summary="ICE coffee futures flat.",
        source="Reuters",
        commodity_tags=["Coffee"],
    )

    mw._add_recent_article(a1)
    mw._add_recent_article(a2)
    mw._add_recent_article(a_no_link)

    print(f"  _recent_articles count = {len(mw._recent_articles)}")
    assert len(mw._recent_articles) == 3

    for i, row in enumerate(mw._recent_articles):
        print(f"  Row {i}: tuple={len(row)} fields, status={row[2]!r}, "
              f"time={row[5]!r}, link={row[6]!r}, read={row[7]}")
        assert len(row) == 8, f"row {i} has {len(row)} fields, expected 8"
        assert row[5] != "—", f"row {i} should have formatted time"
    # a2 is most recent (index 0). a1 follows (index 1). a_no_link at end (index 2).
    assert mw._recent_articles[2][6] == "", "no-link row should have empty link"

    # Inspect the Treeview directly.
    print("\n  Tree rows after push:")
    for item_id in mw.articles_tree.get_children():
        vals = mw.articles_tree.item(item_id, "values")
        tags = mw.articles_tree.item(item_id, "tags")
        print(f"    iid={item_id}: values={vals!r}, tags={tags!r}")
        assert len(vals) == 5, f"row should have 5 visible columns, got {len(vals)}"

    # Check that the "Read more..." label is in the link column.
    tree_values = [mw.articles_tree.item(i, "values") for i in mw.articles_tree.get_children()]
    has_read_more = any("Read more" in (v[4] or "") for v in tree_values)
    print(f"  has 'Read more...' label = {has_read_more}")
    assert has_read_more, "should have 'Read more...' label"

    # Simulate click on link column (column #5) for the first row.
    # Use _on_recent_click directly with a synthetic event.
    mw._on_recent_click(type("E", (), {"x": 800, "y": 50})())  # fake event
    print("\n  _on_recent_click test: no crash")
    mw._mark_all_read(False)
    print("  mark_all unread OK")
    mw._mark_all_read(True)
    print("  mark_all read OK")
    mw._clear_recent_articles()
    print("  clear OK")

    # Test filter still works.
    mw._add_recent_article(a1)
    mw._add_recent_article(a2)
    mw._article_filter_var.set("unread")
    mw._refresh_recent_articles_view()
    n = len(mw.articles_tree.get_children())
    print(f"  filter unread → {n} rows")
    assert n == 2

    print("\n  ALL INTEGRATION TESTS PASS")
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)
finally:
    try:
        root.destroy()
    except Exception:
        pass