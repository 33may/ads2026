"""Plot every recorded repetition; do not pool away baseline variation."""
import argparse
import csv
import json
from pathlib import Path


def plot(session):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    validation = json.loads((session / 'validation.json').read_text())
    if validation['status'] != 'pass':
        raise ValueError('Reconcile this session with audit_phase4.py before plotting.')
    with (session / 'summary.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    colors = ['#2868a0', '#b56a16', '#8257a0']
    fig = plt.figure(figsize=(11, 8), layout='constrained')
    grid = fig.add_gridspec(2, 2, height_ratios=[1.1, 1])
    maximum = max(int(r['failures']) for r in rows)
    for index, (policy, label) in enumerate([('least-connections', 'Least Connections'), ('lrt', 'Least Response Time')]):
        ax = fig.add_subplot(grid[0, index])
        for repetition in (1, 2, 3):
            selected = [next(r for r in rows if r['policy'] == policy and r['ft'] == ft
                             and int(r['repetition']) == repetition) for ft in ('off', 'on')]
            positions = [(repetition - 2) * .23, 1 + (repetition - 2) * .23]
            counts = [int(r['failures']) for r in selected]
            ax.bar(positions, counts, width=.21, color=colors[repetition-1], label=f'Repetition {repetition}')
            ax.scatter(positions, counts, s=14, color=colors[repetition-1], zorder=3)
            for x, count in zip(positions, counts):
                ax.annotate(str(count), (x, count), xytext=(0, 6), textcoords='offset points', ha='center', fontsize=10)
        ax.set_xticks([0, 1], ['FT off', 'FT on'])
        ax.set_title(label, loc='left', fontweight='bold')
        ax.set_ylabel('Failed requests / 1,200 scheduled')
        ax.set_ylim(0, max(20, maximum * 1.35))
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.grid(axis='y', alpha=.18)
        ax.set_axisbelow(True)
        ax.legend(frameon=False, fontsize=8, loc='upper right')

    ax = fig.add_subplot(grid[1, :])
    selected = [r for r in rows if r['ft'] == 'on']
    labels = [('LC' if r['policy'] == 'least-connections' else 'LRT') + f" · r{r['repetition']}" for r in selected]
    for key, color, marker, label in [
        ('observed_detection_s', '#b14f38', 'o', 'First observed unhealthy (from SIGKILL command start)'),
        ('observed_recovery_s', '#267a66', 's', 'First observed healthy (from start command start)'),
    ]:
        values = [float(r[key]) for r in selected]
        offset = -.06 if key == 'observed_detection_s' else .06
        positions = [x + offset for x in range(len(selected))]
        ax.plot(positions, values, linestyle='none', marker=marker, color=color, label=label)
        for x, value in zip(positions, values):
            label_offset = -16 if key == 'observed_detection_s' and value > 1 else 7
            ax.annotate(f'{value:.2f}', (x, value), xytext=(0, label_offset), textcoords='offset points', ha='center', fontsize=9)
    ax.set_xticks(range(len(labels)), labels)
    ax.set_ylim(0, max(float(r['observed_recovery_s']) for r in selected) * 1.42)
    ax.set_ylabel('Observed delay (seconds)')
    ax.set_title('FT on: exclusion and return observations', loc='left', fontweight='bold')
    ax.legend(loc='upper right', fontsize=8, frameon=False)
    ax.grid(axis='y', alpha=.18)
    fig.suptitle('Phase 4 · all 12 runs retained', fontsize=16, fontweight='bold')
    fig.supxlabel('20 queries/s · 20 s normal + 20 s outage + 20 s recovery · expected count 6208\n'
                  'Polling nominally 1 Hz; observed delays include command/polling uncertainty. '
                  'Baseline caveats: see analysis.md.', fontsize=9)
    for extension in ('png', 'pdf'):
        fig.savefig(session / f'comparison.{extension}', dpi=180)
    plt.close(fig)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path)
    plot(parser.parse_args().session)
