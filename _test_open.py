import sys, pathlib, yaml, tkinter as tk, time, threading
sys.path.insert(0, r'C:\Unillog\ThaoResearch')

cfg_path = pathlib.Path(r'C:\Unillog\ThaoResearch\config.yaml')
with open(cfg_path, 'r', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)

root = tk.Tk(); root.withdraw()
from desktop.main_window import MainWindow
from desktop.article_viewer import ArticleViewer
from desktop.gui_queue import gui_queue
from datetime import datetime
from models.article import Article

opened = []
def on_open_article(article):
    print(f'>>> on_open_article called: {article.title!r}')
    opened.append(article)
    viewer = ArticleViewer(article, parent=root)
    viewer.show()
    print('>>> viewer.show() returned')

w = MainWindow(
    config=cfg, config_path=cfg_path,
    on_run_now=None, on_fetch_now=None,
    on_toggle_loop=None, on_clear_cache=None,
    on_open_article=on_open_article,
    on_config_saved=None,
    scheduler=None, job_id='main_job',
)
root.update_idletasks()

# Add an article to recent_articles
art = Article(
    title='Click me',
    link='http://example.com/1',
    pub_date=datetime.now(),
    source='Test',
    summary='Click me to open viewer',
)
gui_queue.put(('new_article', art))
w._poll_gui_queue()
root.update_idletasks()
print('articles:', len(w._recent_articles))
print('first:', w._recent_articles[0][1].title)

# Schedule click + double-click + check if window opens
def click_after():
    print('Selecting row 0')
    w.articles_tree.selection_set(0)
    w.articles_tree.focus(0)
    root.update_idletasks()
    print('Triggering _on_recent_article_open (simulating double-click)')
    w._on_recent_article_open()
    print('After _on_recent_article_open: opened count =', len(opened))
    # Check viewer state
    try:
        windows = root.winfo_children()
        print('Children of root:', windows)
        for c in windows:
            try:
                if c.winfo_class() == 'Toplevel':
                    print('  Toplevel title:', c.title())
            except Exception as e:
                print('  err:', e)
    except Exception as e:
        print('inspect err:', e)
    root.after(500, root.quit)

root.after(200, click_after)
root.mainloop()
print('opened:', len(opened))