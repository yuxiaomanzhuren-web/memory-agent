"""8781 readiness gate: trusted release hashes, exact dependency, real word path."""
from __future__ import annotations
import hashlib
import importlib
import json
from pathlib import Path
import sys

FILES=('memory-8781.ps1','memory-core/pyproject.toml','memory-core/requirements.txt',
       'memory-core/memory_core/server.py','memory-core/memory_core/memory_gates.py',
       'memory-core/memory_core/gate1_recall_units.py','memory-core/memory_core/startup_check.py')
FILES+=tuple('memory-core/memory_core/'+name for name in(
    'operation_ledger.py','store.py','memory_delete.py','memory_retirement.py',
    'memory_pipeline.py','daybook.py','scope_auth.py','delete_audit.py','delete_purge.py'))
FILES+=('memory-core/memory_core/ledger_panel.py','memory-core/memory_core/console.html')
FILES+=('memory-core/memory_core/console_api.py','memory-core/memory_core/console_panel.py')
FILES+=('memory-core/memory_core/notebook.py','memory-core/memory_core/intent.py')
FILES+=tuple('memory-core/memory_core/'+name for name in (
    'adapters/__init__.py', 'adapters/qq/__init__.py', 'memory_evidence.py',
    'rawlog.py', 'source_provenance.py'))
FILES+=('memory-core/memory_core/turn_recall.py','memory-core/memory_core/memory_surface.py')
FILES+=('memory-core/memory_core/citation.py',)

class StartupRefused(RuntimeError):
    def __init__(self, code, message, fix):
        self.details=dict(passed=False,reason_code=code,message=message,fix=fix,python=sys.executable)
        super().__init__(message+'; '+fix)

def check():
    root=Path(__file__).resolve().parents[2]
    manifest_path=Path(__file__).with_name('startup-release.json')
    try:
        manifest=json.loads(manifest_path.read_text('utf-8-sig'))
        if manifest.get('schema_version')!=1 or set(manifest['files'])!=set(FILES):
            raise ValueError('invalid manifest')
    except (OSError,ValueError,KeyError,TypeError):
        raise StartupRefused('release_manifest_missing_or_invalid','启动清单缺失或无效',
            '使用本次交付清单；不得以当前混装文件自动登记或 --accept-change 放行') from None
    mismatches=[name for name in FILES if not (root/name).is_file() or
        hashlib.sha256((root/name).read_bytes()).hexdigest()!=manifest['files'][name]]
    if mismatches:
        raise StartupRefused('release_hash_mismatch','文件版本不齐：'+', '.join(mismatches),
            '按交付补丁校齐文件后重跑；保留现有服务，不切换版本')
    try:
        jieba=importlib.import_module('jieba')
    except ImportError:
        raise StartupRefused('dependency_missing','缺 jieba==0.42.1',
            '"'+sys.executable+'" -m pip install jieba==0.42.1（也可用随包离线 wheel）') from None
    if getattr(jieba,'__version__',None)!='0.42.1':
        raise StartupRefused('dependency_version_mismatch','需要 jieba==0.42.1',
            '在上述服务解释器中安装固定版本，再运行预检')
    package=__package__ or 'memory_core'
    if not __package__:
        sys.path.insert(0,str(root/'memory-core'))
    try:
        units=importlib.import_module(package+'.gate1_recall_units')
        if Path(units.__file__).resolve()!=(root/'memory-core/memory_core/gate1_recall_units.py').resolve():
            raise ValueError('wrong module path')
        for query,required in [('清理哪个盘',{'清理','盘','清理盘'}),
                               ('_validate() 提前校验连线',{'_validate()','校验连线'})]:
            # Exhausts dictionary initialization and the real module call path.
            plan=units.analyze(query,query,lambda unused:set())
            if not required <= set(plan['units']):raise ValueError('word probe failed')
    except Exception as exc:
        raise StartupRefused('tokenizer_probe_failed','实际分词调用失败：'+type(exc).__name__,
            '检查解释器、完整模块和 jieba 字典资源；不要回退旧分词') from None
    return dict(passed=True,release=manifest['release'],python=sys.executable,
        source_root=str(root),checks=['release_hashes','jieba_version','actual_tokenization'])

def main():
    try:
        result=check()
    except StartupRefused as exc:
        result=exc.details
    except Exception as exc:
        result=dict(passed=False,reason_code='preflight_failed',message=type(exc).__name__,
            fix='检查启动清单与服务解释器；保留旧服务',python=sys.executable)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':sys.exit(main())
