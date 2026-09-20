"""Best-effort per-turn reviews, independent of browser lifetime or model output."""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
from agent.filesystem import FileEngine
from agent.coding_reviews import ReviewStore, capture, freeze_review
from shared.util import DevError


class TurnReviews:
    def __init__(self, directory, row, config_path):
        self.directory, self.row, self.config_path = Path(directory), row, Path(config_path)
        self.baselines = {}

    def engine(self):
        # Never create a Journal here: its constructor performs Agent recovery.
        with self.config_path.open(encoding='utf-8') as source:
            config = json.load(source)
        engine = FileEngine(config, SimpleNamespace(directory=self.directory.parent), self.config_path)
        project = {'id': self.row['project_id'], 'alias': self.row['project_id'],
                   'root': self.row['root'], 'device_id': self.row['device_id'],
                   'mode': 'write', 'allow_tasks': True, '_coding_owner': 'native:'+self.row['id']}
        engine.root(project)  # Fresh local root authorization at BOTH ends of a turn.
        return engine, project

    def begin(self, receipt):
        try:
            engine, project = self.engine()
            document = ReviewStore(engine).save(capture(engine, project))
            self.baselines[receipt] = document['id']
        except Exception as exc:  # Optional review failures must not interrupt native execution.
            self.baselines[receipt] = {'code': getattr(exc, 'code', 'REVIEW_UNAVAILABLE')}

    def finish(self, receipt):
        baseline = self.baselines.pop(receipt, None)
        if baseline is None:
            return None
        try:
            if isinstance(baseline, dict):
                return {'available': False, 'status': 'unavailable', 'text': '未能记录修改前快照，不能核实本轮改动。', 'reason': baseline['code']}
            engine, project = self.engine()
            summary = freeze_review(engine, project, baseline)
            # The persistent transcript contains a compact summary; large private
            # source/diff data stays in the bounded review store and loads on demand.
            try:
                ReviewStore(engine).release_baseline(baseline, project)
            except (DevError, OSError):
                pass  # Review is durable; failed cleanup must not hide a valid result.
            coverage = summary['coverage']
            return {**summary, 'available': True,
                    'coverage': {'complete': coverage['complete'],
                                 'skipped_files': len(coverage['before']['skipped'])+len(coverage['after']['skipped'])},
                    'text': '本轮期间改动 · '+str(summary['summary']['files'])+' 个文件'}
        except Exception as exc:  # Optional review failures must not interrupt native execution.
            return {'available': False, 'status': 'unavailable', 'text': '改动审阅暂不可用；不会把未知结果显示为没有修改。',
                    'reason': getattr(exc, 'code', 'REVIEW_UNAVAILABLE')}
