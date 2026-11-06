ThaoResearch 1.0.0 - portable edition
=====================================

Unzip anywhere and run ThaoResearch.exe. Nothing is installed, nothing is
written to the registry.


Where your data is kept
-----------------------

Everything the app creates -- settings, the article history, the price
database, the licence file and the logs -- lives in ONE folder, so that folder
is your whole backup. Zip it, copy it to another PC, unzip, done.

By default that folder is "data", right next to ThaoResearch.exe. That keeps
the whole thing portable on a USB stick.

If the folder holding the .exe is NOT writable -- for example you put it in
C:\Program Files, on a read-only share, or on a locked USB stick -- the app
does NOT fail. It falls back to:

    %LOCALAPPDATA%\ThaoResearch\data

To see exactly what your copy is using, open a Command Prompt here and run:

    ThaoResearch.exe --show-paths


Choosing your own folder
------------------------

Three ways, in order of precedence:

1. In the app:  Settings -> General -> Storage -> "Change..."
   It offers to copy your existing settings, history and licence across, and
   remembers the choice for next time.

2. Edit datadir.txt (see datadir.txt.example in this folder):
   rename it to datadir.txt and put one folder path in it, for example

       D:\ThaoResearchData

   Keep that file next to the .exe and the whole thing stays portable.

3. On the command line, for a one-off run:

       ThaoResearch.exe --data-dir "D:\ThaoResearchData"


If it does not start
--------------------

Run it from a Command Prompt so you can see the output:

    ThaoResearch.exe --show-paths

A failure during startup is also written to:

    <data folder>\logs\startup-error.log

and shown in a message box, so you should never get a silent "nothing
happens" -- if you do, that log is the first place to look.


Telegram alerts
---------------

The bot credentials are built into the .exe; you do not need a .env file.
Alerts arrive in the configured channel. Each one is a short teaser with a
"Read more" button that expands the full article inside Telegram (with
Prev/Next paging for long pieces) and a "Read less" button to collapse it
again. The article link is always in the message.
