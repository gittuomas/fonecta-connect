"""Run the production CLI while prohibiting lab/label/simulator/secret reads."""
from pathlib import Path
import os,runpy,sys
HERE=Path(__file__).resolve().parents[1]
blocked=[];opened=[]
def audit(event,args):
    if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
        path=os.fsdecode(args[0]);parts=Path(path).parts
        if 'lab' in parts or 'labels' in parts or Path(path).name in ['.env','sim_data.json']:
            blocked.append(path);raise RuntimeError('Forbidden inference read: '+path)
        opened.append(path)
sys.addaudithook(audit)
sys.path.insert(0,str(HERE));sys.argv=[str(HERE/'run.py')]+sys.argv[1:]
runpy.run_path(str(HERE/'run.py'),run_name='__main__')
assert not blocked
assert not any(name=='harness' or name=='m0' or name.startswith('exp.') for name in sys.modules)
print('ISOLATION PASS: no lab, labels, simulator or secrets accessed; no research modules imported')
