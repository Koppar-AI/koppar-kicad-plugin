# Changes

## 0.1.2

- **A board check before routing.** Autoroute, route selected nets, finish
  open nets and repair connections first run KiCad's design-rule check on the
  board and stop on what a route cannot fix: shorts (from copper already on the
  board or from parts placed on each other), overlapping parts, and net classes
  whose clearance a footprint's own pads cannot meet. **Fix and route** moves
  the overlapping parts apart, clears the copper around them and removes stray
  vias and tracks before routing, on your account's copy of the board; locked
  parts stay where they are. **Route anyway** routes the board as it is.
- **One project per board.** With an account, Autoroute runs in the same
  Koppar project as every other task on that board, so the run and its result
  are in the Koppar app too. **Stop** ends it and leaves the board as it was,
  and a run that outlives the window is picked up again when it opens.
- **The rules in Board Setup are the ones sent.** Net classes, their
  clearances, track widths, via sizes and differential-pair settings, and the
  board's minimum rules are taken from the editor when a task runs, also when
  they are not saved yet.
- **Updates.** The plugin checks for a newer version each time its window
  opens (on by default) and shows an **Update** button when there is one. Installing updates
  automatically can be switched on in the account dialog. The installed version
  is kept beside the new one.
- **Feedback.** A Feedback button in the window's header, and a link under a
  run that failed, send the Koppar team a message and a rating, and, only when
  ticked, the details of the last run (versions, task, error), shown in the box
  as they are sent. It works without an account. The plugin collects nothing on
  its own.
- **Options in plain words.** Each task shows only the options a board
  designer sets; the long list of tool settings under More options is gone.
  Placement tasks move all unlocked parts or the ones selected in KiCad (lock a
  part to keep it in place); Route selected nets can route differential pairs
  first and allow vias in pads; Rip up can also remove pours and locked copper.
- Parts placed at angles other than 0, 90, 180 or 270 degrees no longer stop
  auto-place when they are not the ones being moved.

## 0.1.1

- The plugin tells Koppar where it was installed from (the Plugin and Content
  Manager, a file, by hand or a source checkout).

## 0.1.0

- First release: Autoroute, and with a Koppar account the board tools and
  Ask Koppar, each result one step on KiCad's undo stack. KiCad 7 to 10.
