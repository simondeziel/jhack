import enum
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from subprocess import Popen, PIPE, STDOUT
from typing import Sequence, Optional, Iterable, List, Dict, Tuple, Union

import parse
import typer
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.table import Table

from jhack.config import JUJU_COMMAND

logger = logging.getLogger(__file__)


@dataclass
class Target:
    app: str
    unit: int
    leader: bool = False

    @staticmethod
    def from_name(name: str):
        app, unit_ = name.split('/')
        leader = unit_.endswith('*')
        unit = unit_.strip('*')
        return Target(app, unit, leader=leader)

    @property
    def unit_name(self):
        return f"{self.app}/{self.unit}"

    def __hash__(self):
        return hash((self.app, self.unit, self.leader))


def get_all_units() -> Sequence[Target]:
    cmd = Popen(f"{JUJU_COMMAND} status".split(' '), stdout=PIPE)
    output = cmd.stdout.read().decode('utf-8')

    units = []
    units_section = False
    for line in output.split('\n'):
        if units_section and not line.strip():
            # empty line after units section: end of units section
            units_section = False
            break

        first_part, *_ = line.split(' ')
        if first_part == 'Unit':
            units_section = True
            continue

        if units_section:
            target = Target.from_name(first_part)
            units.append(target)
    return tuple(units)


def parse_targets(targets: str = None) -> Sequence[Target]:
    if not targets:
        return get_all_units()

    all_units = None  # cache of all units according to juju status

    targets_ = targets.split(';')
    out = set()
    for target in targets_:
        if '/' in target:
            out.add(Target.from_name(target))
        else:
            if not all_units:
                all_units = get_all_units()
            # target is an app name: we need to gather all units of that app
            out.update((u for u in all_units if u.app == target))
    return tuple(out)


class LEVELS(enum.Enum):
    DEBUG = 'DEBUG'
    TRACE = 'TRACE'
    INFO = 'INFO'
    ERROR = 'ERROR'


@dataclass
class EventLogMsg:
    pod_name: str
    timestamp: str
    loglevel: str
    unit: str
    event: str
    # relation: str = None
    # relation_id: str = None


@dataclass
class EventDeferredLogMsg(EventLogMsg):
    event_cls: str
    charm_name: str
    n: str

    # the original event we're deferring or re-deferring
    msg: EventLogMsg = None


@dataclass
class EventReemittedLogMsg(EventLogMsg):
    event_cls: str
    charm_name: str
    n: str

    deferred: EventDeferredLogMsg = None


class Processor:
    # FIXME: why does sometime event/relation_event work, and sometimes
    #  uniter_event does? OF Version?
    event = parse.compile(
        "{pod_name}: {timestamp} {loglevel} unit.{unit}.juju-log Emitting Juju event {event}.")
    relation_event = parse.compile(
        "{pod_name}: {timestamp} {loglevel} unit.{unit}.juju-log {relation}:{relation_id}: Emitting Juju event {event}.")
    uniter_event = parse.compile(
        '{pod_name}: {timestamp} {loglevel} juju.worker.uniter.operation ran "{event}" hook (via hook dispatching script: dispatch)')
    # Deferring <UpdateStatusEvent via TraefikIngressCharm/on/bork[247]>.
    event_deferred = parse.compile(
        '{pod_name}: {timestamp} {loglevel} unit.{unit}.juju-log Deferring <{event_cls} via {charm_name}/on/{event}[{n}]>.')
    # unit-traefik-k8s-0: 12:16:47 DEBUG unit.traefik-k8s/0.juju-log Re-emitting <UpdateStatusEvent via TraefikIngressCharm/on/update_status[130]>.
    event_reemitted = parse.compile(
        '{pod_name}: {timestamp} {loglevel} unit.{unit}.juju-log Re-emitting <{event_cls} via {charm_name}/on/{event}[{n}]>.'
    )

    def __init__(self, targets: Iterable[Target],
                 add_new_targets: bool = True,
                 history_length: int = 10,
                 show_defer: bool = False):
        self.targets = list(targets)
        self.add_new_targets = add_new_targets
        self.history_length = history_length
        self.console = console = Console()
        self.table = table = Table(show_footer=False, expand=True)
        self._unit_grids = {}
        self._track_deferrals = show_defer

        table.add_column(header="timestamp")
        unit_grids = []
        for target in targets:
            tgt_grid = Table.grid('', '', expand=True)
            table.add_column(header=target.unit_name)
            self._unit_grids[target.unit_name] = tgt_grid
            unit_grids.append(tgt_grid)

        self._timestamps_grid = Table.grid('', expand=True)
        table.add_row(self._timestamps_grid, *unit_grids)

        self.table_centered = table_centered = Align.center(table)
        self.live = Live(table_centered, console=console,
                         screen=False, refresh_per_second=20)

        self.evt_count = 0
        self._lanes = {}
        self.tracking: Dict[str, List[EventLogMsg]] = {tgt.unit_name: [] for tgt
                                                       in targets}
        self._deferred: Dict[str, List[EventDeferredLogMsg]] = defaultdict(list)

    def _track(self, evt: EventLogMsg):
        if self.add_new_targets and evt.unit not in self.tracking:
            self._add_new_target(evt)

        if evt.unit in self.tracking:  # target tracked
            self.evt_count += 1
            self.tracking[evt.unit].append(evt)
            print(f"tracking {evt.event}")

    def _defer(self, deferred: EventDeferredLogMsg):
        # find the original message we're deferring
        def _search_in_deferred():
            for dfrd in self._deferred[deferred.unit]:
                if dfrd.n == deferred.n:
                    # not the first time we defer this boy
                    return dfrd.msg

        def _search_in_tracked():
            for msg in self.tracking.get(deferred.unit, ()):
                if msg.event == deferred.event:
                    return msg

        msg = _search_in_deferred() or _search_in_tracked()
        deferred.msg = msg
        if not isinstance(msg, EventDeferredLogMsg):
            self.evt_count += 1

        self._deferred[deferred.unit].append(deferred)
        print(f"deferred {deferred.event}")

    def _reemit(self, reemitted: EventReemittedLogMsg):
        # search deferred queue first to last
        unit = reemitted.unit
        for defrd in list(self._deferred[unit]):
            if defrd.n == reemitted.n:
                reemitted.deferred = defrd
                self._deferred[unit].remove(defrd)
                # we track it.
                self.tracking[unit].append(reemitted)
                print(f"reemitted {reemitted.event}")
                return

        raise RuntimeError(
            f"cannot reemit {reemitted.event}({reemitted.n}); no "
            f"matching deferred event could be found "
            f"in {self._deferred[unit]}.")

    def __enter__(self):
        self.live.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.live.__exit__(exc_type, exc_val, exc_tb)

    def _match_event_deferred(self, log: str) -> Optional[EventDeferredLogMsg]:
        match = self.event_deferred.parse(log)
        if match:
            return EventDeferredLogMsg(**match.named)

    def _match_event_reemitted(self, log: str) -> Optional[
        EventReemittedLogMsg]:
        match = self.event_reemitted.parse(log)
        if match:
            return EventReemittedLogMsg(**match.named)

    def _match_event_emitted(self, log: str) -> Optional[EventLogMsg]:
        # log format =
        # unit-traefik-k8s-0: 10:36:19 DEBUG unit.traefik-k8s/0.juju-log ingress-per-unit:38: Emitting Juju event ingress_per_unit_relation_changed.
        # unit-prometheus-k8s-0: 13:06:09 DEBUG unit.prometheus-k8s/0.juju-log ingress:44: Emitting Juju event ingress_relation_changed.

        match = self.event.parse(log)

        # search for relation events
        if not match:
            match = self.relation_event.parse(log)
            if match:
                params = match.named
                # we don't have any use for those (yet?).
                del params['relation']
                del params['relation_id']

        # attempt to match in another format ?
        if not match:
            # fallback
            if match := self.uniter_event.parse(log):
                unit = parse.compile("unit-{}").parse(match.named['pod_name'])
                params = match.named
                *names, number = unit.fixed[0].split('-')
                name = '-'.join(names)
                params['unit'] = '/'.join([name, number])
            else:
                return

        else:
            params = match.named

        # uniform
        params['event'] = params['event'].replace('-', '_')
        return EventLogMsg(**params)

    def _add_new_target(self, msg: EventLogMsg):
        logger.info(f"adding new unit {msg.unit}")
        new_target = Target.from_name(msg.unit)

        self.tracking[msg.unit] = []
        self.targets.append(new_target)
        self.table.add_column(header=new_target.unit_name)
        grid = Table.grid('', expand=True)

        self._unit_grids[new_target.unit_name] = grid
        self.table.columns[-1]._cells.append(grid)

    def process(self, log: str):
        """process a log line"""
        if msg := self._match_event_emitted(log):
            mode = 'emit'
            self._track(msg)
        elif self._track_deferrals:
            if msg := self._match_event_deferred(log):
                mode = 'defer'
            elif msg := self._match_event_reemitted(log):
                self._track(msg)
                mode = 'reemit'
            else:
                return
        else:
            return

        if not self._is_tracking(msg):
            return

        if mode == 'emit':
            self.display(msg)
        elif mode == 'defer':
            self._defer(msg)
        elif mode == 'reemit':
            self._reemit(msg)
            self.display(msg, reemitted=True)

        if self._track_deferrals and self._is_tracking(msg) and mode != 'emit':
            self.update(msg)

    def _is_tracking(self, msg):
        return msg.unit in self.tracking

    _pad = " "
    _dpad = _pad * 2
    _nothing_to_report = "."
    _vline = "│"
    _cross = "┼"
    _lup = "┘"
    _lupdown = "┤"
    _bounce = "⭘"
    _ldown = "┐"
    _hline = "─"
    _close = "❮"
    _open = "❯"

    def update(self, msg: EventLogMsg):
        # all the events we presently know to be deferred
        unit = msg.unit
        grid = self._unit_grids[unit]
        deferred = self._deferred[unit]
        reemitting = isinstance(msg, EventReemittedLogMsg)

        tail = self._vline * len(tuple(d for d in deferred if d is not msg))

        if isinstance(msg, EventDeferredLogMsg):
            try:
                previous_msg_idx = next(
                    filter(
                        lambda s: s[1].startswith(f"({msg.n})"),
                        enumerate(grid.columns[0]._cells))
                )[0]
            except StopIteration:
                previous_msg_idx = None

            if previous_msg_idx == None:
                previous_msg_idx = next(
                    filter(
                        lambda s: s[1] == msg.event,
                        enumerate(grid.columns[0]._cells))
                )[0]
                grid.columns[0]._cells[
                    previous_msg_idx] = f"({msg.n}) {msg.event}"
                original_cell = grid.columns[1]._cells[previous_msg_idx]
                new_cell = original_cell.replace(
                    self._dpad,
                    self._open + self._hline).replace(
                    self._vline, self._cross) + self._lup
                grid.columns[1]._cells[previous_msg_idx] = new_cell
                lane = new_cell.index(self._lup)

            else:
                # not the first time we defer you, boy
                original_cell = grid.columns[1]._cells[previous_msg_idx]
                new_cell = original_cell.replace(
                    self._close + self._hline, self._pad + self._bounce
                ).replace(self._ldown, self._lupdown) + tail
                grid.columns[1]._cells[previous_msg_idx] = new_cell
                lane = new_cell.index(self._lupdown)

            self._cache_lane(msg.n, lane)

        elif isinstance(msg, EventReemittedLogMsg):
            previous_reemit = None
            try:
                previous_reemit = next(
                filter(
                    lambda s: s[1].startswith(f"({msg.n})"),
                    enumerate(grid.columns[0]._cells[1:]))
                )[0] + 1
            except StopIteration:
                # message must have been cropped away
                logger.debug(f'unable to grab fetch previous reemit, '
                             f'msg {msg.n} must be out of scope')

            cell = None
            if previous_reemit is not None:
                previous_cell = grid.columns[1]._cells[previous_reemit]

                # reopen previous reemittal if it's closed
                cell = previous_cell.replace(
                    self._close + self._hline,
                    self._pad + self._bounce
                ).replace(
                    self._ldown, self._lupdown)
                grid.columns[1]._cells[previous_reemit] = cell

            # now we look at the newly added cell and add a closure statement.
            new_cell = grid.columns[1]._cells[0]
            new_cell = new_cell.replace(
                self._dpad, self._close + self._hline)
            new_cell += tail

            lane = self._get_lane(msg.n)
            if not lane:
                if cell is None:
                    raise RuntimeError(f'lane not cached for {msg.n}, and '
                                       f'message is out of scope. '
                                       f'Unable to proceed.')

                if self._lup in cell:
                    lane = cell.index(self._lup)
                else:
                    lane = cell.index(self._lupdown)
                self._cache_lane(msg.n, lane)

            closed_cell = _put(new_cell, lane, self._ldown, self._hline)
            final_cell = list(closed_cell)
            for ln in range(lane):
                if final_cell[ln] == self._vline:
                    final_cell[ln] = self._cross
            grid.columns[1]._cells[0] = ''.join(final_cell)


            if previous_reemit is not None:
                rng = range(1, previous_reemit)
            else:
                # until the end of the visible table
                rng = range(1, len(grid.columns[1]._cells))

            for ln in rng:
                grid.columns[1]._cells[ln] = _put(
                    grid.columns[1]._cells[ln], lane,
                    {None: self._vline,
                     self._hline: self._cross,
                     self._ldown: self._lupdown},
                self._nothing_to_report)

        else:
            grid.columns[1]._cells[0] += tail

    def _get_lane(self, n: str):
        return self._lanes.get(n)

    def _cache_lane(self, n: str, lane: int):
        self._lanes[n] = lane

    def display(self, msg: EventLogMsg, reemitted: bool = False):
        unit_grid = self._unit_grids[msg.unit]
        n = f"({msg.n}) " if reemitted else ""
        unit_grid.add_row(f"{n}{msg.event}", '  ')
        # move last to first, because we can't add row to the top
        self._rotate(unit_grid, 0)
        self._rotate(unit_grid, 1)

        self._add_timestamp(msg)
        self._crop()

    @staticmethod
    def _rotate(table, column):
        table.columns[column]._cells.insert(0, table.columns[
            column]._cells.pop())  # noqa

    def _add_timestamp(self, msg: EventLogMsg):
        timestamps = self._timestamps_grid
        timestamps.add_row(msg.timestamp)
        # move last to first, because we can't add row to the top
        self._rotate(timestamps, 0)

    def _crop(self):
        # crop all:
        for table in (self._timestamps_grid, *self._unit_grids.values()):
            if len(table.rows) > self.history_length:
                logger.info('popping a row...')
                for column in table.columns:
                    column._cells.pop()  # pop last
                table.rows.pop()  # pop last


def _get_debug_log(cmd):
    return Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=STDOUT)


def tail_events(
        targets: str = typer.Argument(
            None,
            help="Semicolon-separated list of targets to follow. "
                 "Example: 'foo/0;foo/1;bar/2'. By default, it will follow all "
                 "available targets."),
        add_new_targets: bool = True,
        level: LEVELS = 'DEBUG',
        replay: bool = True,  # listen from beginning of time?
        dry_run: bool = False,
        framerate: float = .5,
        length: int = typer.Option(10, '-n', '--length'),
        show_defer: bool = False,
        watch: bool = True
):
    """Pretty-print a table with the events that are fired on juju units
    in the current model.
    """
    if isinstance(level, str):
        level = getattr(LEVELS, level.upper())

    if not isinstance(level, LEVELS):
        raise ValueError(level)

    track_events = True
    if level not in {LEVELS.DEBUG, LEVELS.TRACE}:
        print(f"we won't be able to track events with level={level}")
        track_events = False

    if targets and add_new_targets:
        print('targets provided; overruling add_new_targets param.')
        add_new_targets = False

    targets = parse_targets(targets)

    cmd = ([JUJU_COMMAND, 'debug-log'] +
           (['--tail'] if watch else []) +
           (['--replay'] if replay else []) +
           ['--level', level.value])

    if dry_run:
        print(' '.join(cmd))
        return

    try:
        with Processor(targets, add_new_targets,
                       history_length=length,
                       show_defer=show_defer) as processor:
            proc = _get_debug_log(cmd)
            # when we're in replay mode we're catching up with the replayed logs
            # so we won't limit the framerate and just flush the output
            replay_mode = True

            if not watch:
                stdout = iter(proc.stdout.readlines())

                def next_line():
                    try:
                        return next(stdout)
                    except StopIteration:
                        return ''

            else:
                def next_line():
                    line = proc.stdout.readline()
                    return line

            while True:
                start = time.time()

                line = next_line()
                if not line:
                    if not watch:
                        break

                    if proc.poll() is not None:
                        # process terminated FIXME: this shouldn't happen
                        break

                    replay_mode = False
                    continue

                if line:
                    msg = line.decode('utf-8').strip()
                    processor.process(msg)

                if not replay_mode and (
                        elapsed := time.time() - start) < framerate:
                    time.sleep(framerate - elapsed)
                    print(f"sleeping {framerate - elapsed}")


    except KeyboardInterrupt:
        print('exiting...')
        return

    print(f"processed {processor.evt_count} events.")


def _put(s: str, index: int, char: Union[str, Dict[str, str]], placeholder=' '):
    if isinstance(char, str):
        char = {None: char}

    if len(s) <= index:
        s += placeholder * (index - len(s)) + char[None]
        return s

    l = list(s)
    l[index] = char.get(l[index], char[None])
    return ''.join(l)


if __name__ == '__main__':
    tail_events(targets='traefik-k8s/0', length=10000, watch=False)
