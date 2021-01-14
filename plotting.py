import argparse
import collections
import functools
import itertools
import json
import multiprocessing as mp
import os
import pathlib
import re
import subprocess
import warnings

os.environ['NO_AT_BRIDGE'] = '1'  # Hide X org false warning.

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

np.set_string_function(lambda x: f'<np.array shape={x.shape} dtype={x.dtype}>')

Run = collections.namedtuple('Run', 'task method seed xs ys')

PALETTES = dict(
    discrete=(
        '#377eb8', '#4daf4a', '#984ea3', '#e41a1c', '#ff7f00', '#a65628',
        '#f781bf', '#888888', '#a6cee3', '#b2df8a', '#cab2d6', '#fb9a99',
    ),
    contrast=(
        '#0022ff', '#33aa00', '#ff0011', '#ddaa00', '#cc44dd', '#0088aa',
        '#001177', '#117700', '#990022', '#885500', '#553366', '#006666',
    ),
    gradient=(
        '#fde725', '#a0da39', '#4ac16d', '#1fa187', '#277f8e', '#365c8d',
        '#46327e', '#440154',
    ),
    baselines=(
        '#222222', '#666666', '#aaaaaa', '#cccccc',
    ),
)

LEGEND = dict(
    fontsize='medium', numpoints=1, labelspacing=0, columnspacing=1.2,
    handlelength=1.5, handletextpad=0.5, ncol=4, loc='lower center')

DEFAULT_BASELINES = [
    'd4pg', 'dqn_sticky', 'rainbow_sticky', 'human$', 'impala']

BINS = collections.defaultdict(int)
BINS.update(dmc=1e5, atari=1e6)


def find_keys(args):
  filenames = []
  for indir in args.indir:
    task = next(indir.iterdir())  # First only.
    for method in task.iterdir():
      seed = next(indir.iterdir())  # First only.
      filenames += list(seed.glob('**/*.jsonl'))
  keys = set()
  for filename in filenames:
    keys |= set(load_jsonl(filename).columns)
  print(f'Keys      ({len(keys)}):', ', '.join(keys), flush=True)


def load_runs(args):
  total, toload = [], []
  for indir in args.indir:
    filenames = list(indir.glob('**/*.jsonl'))
    total += filenames
    for filename in filenames:
      task, method, seed = filename.relative_to(indir).parts[:-1]
      if not any(p.search(task) for p in args.tasks):
        continue
      if not any(p.search(method) for p in args.methods):
        continue
      toload.append((filename, indir))
  print(f'Loading {len(toload)} of {len(total)} runs...')
  jobs = [functools.partial(load_run, f, i, args) for f, i in toload]
  # Disable async data loading:
  # runs = [j() for j in jobs]
  with mp.Pool(10) as pool:
    promises = [pool.apply_async(j) for j in jobs]
    runs = [p.get() for p in promises]
  runs = [r for r in runs if r is not None]
  return runs


def load_run(filename, indir, args):
  task, method, seed = filename.relative_to(indir).parts[:-1]
  if task == 'atari_jamesbond':
    task = 'atari_james_bond'
  prefix = f'indir{args.indir.index(indir)+1}_'
  seed = prefix + seed
  if args.prefix:
    method = prefix + method
  df = load_jsonl(filename)
  if df is None:
    print('Skipping empty run')
    return
  try:
    df = df[[args.xaxis, args.yaxis]].dropna()
    if args.maxval:
      df = df.replace([+np.inf], +args.maxval)
      df = df.replace([-np.inf], -args.maxval)
      df[args.yaxis] = df[args.yaxis].clip(-args.maxval, +args.maxval)
  except KeyError:
    return
  xs = df[args.xaxis].to_numpy()
  ys = df[args.yaxis].to_numpy()
  bins = BINS[task.split('_')[0]] if args.bins == -1 else args.bins
  if bins:
    borders = np.arange(0, xs.max() + 1e-8, bins)
  xs, ys = bin_scores(xs, ys, borders)
  if not len(xs):
    print('Skipping empty run', task, method, seed)
    return
  return Run(task, method, seed, xs, ys)


def load_baselines(patterns, prefix=False):
  runs = []
  directory = pathlib.Path(__file__).parent / 'scores'
  for filename in directory.glob('**/*_baselines.json'):
    for task, methods in json.loads(filename.read_text()).items():
      for method, score in methods.items():
        if prefix:
          method = f'baseline_{method}'
        if not any(p.search(method) for p in patterns):
          continue
        runs.append(Run(task, method, None, None, score))
  return runs


def stats(runs, baselines):
  tasks = sorted(set(r.task for r in runs))
  methods = sorted(set(r.method for r in runs))
  seeds = sorted(set(r.seed for r in runs))
  baseline = sorted(set(r.method for r in baselines))
  print('Loaded', len(runs), 'runs.')
  print(f'Tasks     ({len(tasks)}):', ', '.join(tasks))
  print(f'Methods   ({len(methods)}):', ', '.join(methods))
  print(f'Seeds     ({len(seeds)}):', ', '.join(seeds))
  print(f'Baselines ({len(baseline)}):', ', '.join(baseline))


def order_methods(runs, baselines, args):
  methods = []
  for pattern in args.methods:
    for method in sorted(set(r.method for r in runs)):
      if pattern.search(method):
        if method not in methods:
          methods.append(method)
        if method not in args.colors:
          index = len(args.colors) % len(args.palette)
          args.colors[method] = args.palette[index]
  non_baseline_colors = len(args.colors)
  for pattern in args.baselines:
    for method in sorted(set(r.method for r in baselines)):
      if pattern.search(method):
        if method not in methods:
          methods.append(method)
        if method not in args.colors:
          index = len(args.colors) - non_baseline_colors
          index = index % len(PALETTES['baselines'])
          args.colors[method] = PALETTES['baselines'][index]
  return methods


def figure(runs, methods, args):
  tasks = sorted(set(r.task for r in runs if r.xs is not None))
  rows = int(np.ceil((len(tasks) + len(args.add)) / args.cols))
  figsize = args.size[0] * args.cols, args.size[1] * rows
  fig, axes = plt.subplots(rows, args.cols, figsize=figsize)
  for task, ax in zip(tasks, axes.flatten()):
    relevant = [r for r in runs if r.task == task]
    plot(task, ax, relevant, methods, args)
  for name, ax in zip(args.add, axes.flatten()[len(tasks):]):
    ax.set_facecolor((0.9, 0.9, 0.9))
    if name == 'gamer_median':
      plot_combined(
          'combined_gamer_median', ax, runs, methods, args,
          lo='random', hi='human$',
          agg=lambda x: np.nanmedian(x, -1))
    elif name == 'gamer_mean':
      plot_combined(
          'combined_gamer_mean', ax, runs, methods, args,
          lo='random', hi='human$',
          agg=lambda x: np.nanmean(x, -1))
    elif name == 'record_mean':
      plot_combined(
          'combined_record_mean', ax, runs, methods, args,
          lo='random', hi='record',
          agg=lambda x: np.nanmean(x, -1))
    elif name == 'clipped_record_mean':
      plot_combined(
          'combined_clipped_record_mean', ax, runs, methods, args,
          lo='random', hi='record', clip=True,
          agg=lambda x: np.nanmean(x, -1))
    elif name == 'num_seeds':
      plot_combined(
          'combined_num_seeds', ax, runs, methods, args,
          agg=lambda x: np.isfinite(x).sum(-1))
    elif name == 'human_above':
      plot_combined(
          'combined_above_human$', ax, runs, methods, args,
          agg=lambda y: (y >= 1.0).astype(float).sum(-1))
    elif name == 'human_below':
      plot_combined(
          'combined_below_human$', ax, runs, methods, args,
          agg=lambda y: (y <= 1.0).astype(float).sum(-1))
    else:
      raise NotImplementedError(name)
  if args.xlim:
    for ax in axes[:-1].flatten():
      ax.xaxis.get_offset_text().set_visible(False)
  if args.xlabel:
    for ax in axes[-1]:
      ax.set_xlabel(args.xlabel)
  if args.ylabel:
    for ax in axes[:, 0]:
      ax.set_ylabel(args.ylabel)
  for ax in axes.flatten()[len(tasks) + len(args.add):]:
    ax.axis('off')
  legend(fig, args.labels, **LEGEND)
  return fig


def plot(task, ax, runs, methods, args):
  assert runs
  try:
    name = TASK_MAPPING.get(task, task)
    title = name.split('_', 1)[1].replace('_', ' ').title()
  except IndexError:
    title = task.title()
  ax.set_title(title)
  xlim = [+np.inf, -np.inf]
  for index, method in enumerate(methods):
    relevant = [r for r in runs if r.method == method]
    if not relevant:
      continue
    if any(r.xs is None for r in relevant):
      baseline(index, method, ax, relevant, args)
    else:
      if args.aggregate == 'none':
        xs, ys = curve_lines(index, task, method, ax, relevant, args)
      else:
        xs, ys = curve_area(index, task, method, ax, relevant, args)
      if len(xs) == len(ys) == 0:
        print(f'Skipping empty: {task} {method}')
        continue
      xlim = [min(xlim[0], xs.min()), max(xlim[1], xs.max())]
  ax.ticklabel_format(axis='x', style='sci', scilimits=(0, 0))
  steps = [1, 2, 2.5, 5, 10]
  ax.xaxis.set_major_locator(ticker.MaxNLocator(args.xticks, steps=steps))
  ax.yaxis.set_major_locator(ticker.MaxNLocator(args.yticks, steps=steps))
  if np.isfinite(xlim).all():
    ax.set_xlim(args.xlim or xlim)
  if args.xlim:
    ticks = sorted({*ax.get_xticks(), *args.xlim})
    ticks = [x for x in ticks if args.xlim[0] <= x <= args.xlim[1]]
    ax.set_xticks(ticks)
  if args.ylim:
    ax.set_ylim(args.ylim)
    if args.ylimticks:
      ticks = sorted({*ax.get_yticks(), *args.ylim})
      ticks = [x for x in ticks if args.ylim[0] <= x <= args.ylim[1]]
      ax.set_yticks(ticks)


def plot_combined(
    name, ax, runs, methods, args, agg, lo=None, hi=None, clip=False):
  tasks = sorted(set(run.task for run in runs if run.xs is not None))
  seeds = list(set(run.seed for run in runs))
  runs = [r for r in runs if r.task in tasks]  # Discard unused baselines.
  # Bin all runs onto the same X steps.
  borders = sorted(
      [r.xs for r in runs if r.xs is not None],
      key=lambda x: np.nanmax(x))[-1]
  for index, run in enumerate(runs):
    if run.xs is None:
      continue
    xs, ys = bin_scores(run.xs, run.ys, borders)
    runs[index] = run._replace(xs=xs, ys=ys)
  # Per-task normalization by low and high baseline.
  if lo or hi:
    mins = collections.defaultdict(list)
    maxs = collections.defaultdict(list)
    [mins[r.task].append(r.ys) for r in load_baselines([re.compile(lo)])]
    [maxs[r.task].append(r.ys) for r in load_baselines([re.compile(hi)])]
    mins = {task: min(ys) for task, ys in mins.items() if task in tasks}
    maxs = {task: max(ys) for task, ys in maxs.items() if task in tasks}
    missing_baselines = []
    for task in tasks:
      if task not in mins or task not in maxs:
        missing_baselines.append(task)
    if set(missing_baselines) == set(tasks):
        print(f'No baselines found to normalize any tasks in {name} plot.')
    else:
      for task in missing_baselines:
        print(f'No baselines found to normalize {task} in {name} plot.')
    for index, run in enumerate(runs):
      if run.task not in mins or run.task not in maxs:
        continue
      ys = (run.ys - mins[run.task]) / (maxs[run.task] - mins[run.task])
      if clip:
        ys = np.minimum(ys, 1.0)
      runs[index] = run._replace(ys=ys)
  # Aggregate across tasks but not methods or seeds.
  combined = []
  for method, seed in itertools.product(methods, seeds):
    relevant = [r for r in runs if r.method == method and r.seed == seed]
    if not relevant:
      continue
    if relevant[0].xs is None:
      xs, ys = None, np.array([r.ys for r in relevant])
    else:
      xs, ys = stack_scores(*zip(*[(r.xs, r.ys) for r in relevant]))
    with warnings.catch_warnings():  # Ignore empty slice warnings.
      warnings.simplefilter('ignore', category=RuntimeWarning)
      combined.append(Run('combined', method, seed, xs, agg(ys)))
  plot(name, ax, combined, methods, args)


def curve_lines(index, task, method, ax, runs, args):
  zorder = 10000 - 10 * index - 1
  for run in runs:
    color = args.colors[method]
    ax.plot(run.xs, run.ys, label=method, color=color, zorder=zorder)
  return runs[0].xs, runs[0].ys


def curve_area(index, task, method, ax, runs, args):
  xs, ys = stack_scores(*zip(*[(r.xs, r.ys) for r in runs]))
  with warnings.catch_warnings():  # NaN buckets remain NaN.
    warnings.simplefilter('ignore', category=RuntimeWarning)
    if args.aggregate == 'std1':
      mean, std = np.nanmean(ys, -1), np.nanstd(ys, -1)
      lo, mi, hi = mean - std, mean, mean + std
    elif args.aggregate == 'per0':
      lo, mi, hi = [np.nanpercentile(ys, k, -1) for k in (0, 50, 100)]
    elif args.aggregate == 'per5':
      lo, mi, hi = [np.nanpercentile(ys, k, -1) for k in (5, 50, 95)]
    elif args.aggregate == 'per25':
      lo, mi, hi = [np.nanpercentile(ys, k, -1) for k in (25, 50, 75)]
    else:
      raise NotImplementedError(args.aggregate)
  color = args.colors[method]
  kw = dict(color=color, zorder=1000 - 10 * index, alpha=0.1, linewidths=0)
  ax.fill_between(xs, lo, hi, **kw)
  ax.plot(xs, mi, label=method, color=color, zorder=10000 - 10 * index - 1)
  return xs, mi


def baseline(index, method, ax, runs, args):
  assert all(run.xs is None for run in runs)
  ys = np.array([run.ys for run in runs])
  mean, std = ys.mean(), ys.std()
  color = args.colors[method]
  kw = dict(color=color, zorder=500 - 20 * index - 1, alpha=0.1, linewidths=0)
  ax.fill_between([-np.inf, np.inf], [mean - std] * 2, [mean + std] * 2, **kw)
  kw = dict(ls='--', color=color, zorder=5000 - 10 * index - 1)
  ax.axhline(mean, label=method, **kw)


def legend(fig, mapping=None, **kwargs):
  entries = {}
  for ax in fig.axes:
    for handle, label in zip(*ax.get_legend_handles_labels()):
      if mapping and label in mapping:
        label = mapping[label]
      entries[label] = handle
  leg = fig.legend(entries.values(), entries.keys(), **kwargs)
  leg.get_frame().set_edgecolor('white')
  extent = leg.get_window_extent(fig.canvas.get_renderer())
  extent = extent.transformed(fig.transFigure.inverted())
  yloc, xloc = kwargs['loc'].split()
  y0 = dict(lower=extent.y1, center=0, upper=0)[yloc]
  y1 = dict(lower=1, center=1, upper=extent.y0)[yloc]
  x0 = dict(left=extent.x1, center=0, right=0)[xloc]
  x1 = dict(left=1, center=1, right=extent.x0)[xloc]
  fig.tight_layout(rect=[x0, y0, x1, y1], h_pad=0.5, w_pad=0.5)


def save(fig, args):
  args.outdir.mkdir(parents=True, exist_ok=True)
  filename = args.outdir / 'curves.png'
  fig.savefig(filename, dpi=args.dpi)
  print('Saved to', filename)
  filename = args.outdir / 'curves.pdf'
  fig.savefig(filename)
  try:
    subprocess.call(['pdfcrop', str(filename), str(filename)])
  except FileNotFoundError:
    print('Install texlive-extra-utils to crop PDF outputs.')


def bin_scores(xs, ys, borders, reducer=np.nanmean):
  order = np.argsort(xs)
  xs, ys = xs[order], ys[order]
  binned = []
  with warnings.catch_warnings():  # Empty buckets become NaN.
    warnings.simplefilter('ignore', category=RuntimeWarning)
    for start, stop in zip(borders[:-1], borders[1:]):
      left = (xs <= start).sum()
      right = (xs <= stop).sum()
      binned.append(reducer(ys[left:right]))
  return borders[1:], np.array(binned)


def stack_scores(multiple_xs, multiple_ys):
  longest_xs = sorted(multiple_xs, key=lambda x: len(x))[-1]
  multiple_padded_ys = []
  for xs, ys in zip(multiple_xs, multiple_ys):
    assert (longest_xs[:len(xs)] == xs).all(), (list(xs), list(longest_xs))
    padding = [np.inf] * (len(longest_xs) - len(xs))
    padded_ys = np.concatenate([ys, padding])
    multiple_padded_ys.append(padded_ys)
  stacked_ys = np.stack(multiple_padded_ys, -1)
  return longest_xs, stacked_ys


def load_jsonl(filename):
  try:
    with filename.open() as f:
      lines = list(f.readlines())
    records = []
    for index, line in enumerate(lines):
      try:
        records.append(json.loads(line))
      except Exception:
        if index == len(lines) - 1:
          continue  # Silently skip last line if it is incomplete.
        raise ValueError(
            f'Skipping invalid JSON line ({index+1}/{len(lines)+1}) in'
            f'{filename}: {line}')
    return pd.DataFrame(records)
  except ValueError as e:
    print('Invalid', filename, e)
    return None


def save_runs(runs, filename):
  filename.parent.mkdir(parents=True, exist_ok=True)
  records = []
  for run in runs:
    if run.xs is None:
      continue
    records.append(dict(
        task=run.task, method=run.method, seed=run.seed,
        xs=run.xs.tolist(), ys=run.ys.tolist()))
  runs = json.dumps(records)
  filename.write_text(runs)
  print('Saved', filename)


def main(args):
  find_keys(args)
  runs = load_runs(args)
  save_runs(runs, args.outdir / 'runs.jsonl')
  baselines = load_baselines(args.baselines, args.prefix)
  stats(runs, baselines)
  methods = order_methods(runs, baselines, args)
  if not runs:
    print('Noting to plot.')
    return
  print('Plotting...')
  fig = figure(runs + baselines, methods, args)
  save(fig, args)


def parse_args():
  boolean = lambda x: bool(['False', 'True'].index(x))
  parser = argparse.ArgumentParser()
  parser.add_argument('--indir', nargs='+', type=pathlib.Path, required=True)
  parser.add_argument('--outdir', type=pathlib.Path, required=True)
  parser.add_argument('--subdir', type=boolean, default=True)
  parser.add_argument('--xaxis', type=str, required=True)
  parser.add_argument('--yaxis', type=str, required=True)
  parser.add_argument('--tasks', nargs='+', default=[r'.*'])
  parser.add_argument('--methods', nargs='+', default=[r'.*'])
  parser.add_argument('--baselines', nargs='+', default=DEFAULT_BASELINES)
  parser.add_argument('--prefix', type=boolean, default=False)
  parser.add_argument('--bins', type=float, default=-1)
  parser.add_argument('--aggregate', type=str, default='std1')
  parser.add_argument('--size', nargs=2, type=float, default=[2.5, 2.3])
  parser.add_argument('--dpi', type=int, default=80)
  parser.add_argument('--cols', type=int, default=6)
  parser.add_argument('--xlim', nargs=2, type=float, default=None)
  parser.add_argument('--ylim', nargs=2, type=float, default=None)
  parser.add_argument('--ylimticks', type=boolean, default=True)
  parser.add_argument('--xlabel', type=str, default=None)
  parser.add_argument('--ylabel', type=str, default=None)
  parser.add_argument('--xticks', type=int, default=6)
  parser.add_argument('--yticks', type=int, default=5)
  parser.add_argument('--labels', nargs='+', default=None)
  parser.add_argument('--palette', nargs='+', default=['contrast'])
  parser.add_argument('--colors', nargs='+', default={})
  parser.add_argument('--maxval', type=float, default=0)
  parser.add_argument('--add', nargs='+', type=str, default=[
      'gamer_median', 'gamer_mean', 'record_mean',
      'clipped_record_mean', 'num_seeds'])
  args = parser.parse_args()
  if args.subdir:
    args.outdir /= args.indir[0].stem
  args.indir = [d.expanduser() for d in args.indir]
  args.outdir = args.outdir.expanduser()
  if args.labels:
    assert len(args.labels) % 2 == 0
    args.labels = {k: v for k, v in zip(args.labels[:-1], args.labels[1:])}
  if args.colors:
    assert len(args.colors) % 2 == 0
    args.colors = {k: v for k, v in zip(args.colors[:-1], args.colors[1:])}
  args.tasks = [re.compile(p) for p in args.tasks]
  args.methods = [re.compile(p) for p in args.methods]
  args.baselines = [re.compile(p) for p in args.baselines]
  if 'return' not in args.yaxis:
    args.baselines = []
  if args.prefix is None:
    args.prefix = len(args.indir) > 1
  if len(args.palette) == 1 and args.palette[0] in PALETTES:
    args.palette = 10 * PALETTES[args.palette[0]]
  if len(args.add) == 1 and args.add[0] == 'none':
    args.add = []
  return args


if __name__ == '__main__':
  main(parse_args())
