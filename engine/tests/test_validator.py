"""Guardrail regressions: malformed/duplicate predictions must never be accepted."""
from pathlib import Path
import csv,sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from validate_submission import valid_hours,validate

class ValidationTests(unittest.TestCase):
    def test_hours(self):
        for s in ['closed','09:00-17:00','18:00-02:00','09:00-12:00,13:00-18:00','00:00-23:59']:
            self.assertTrue(valid_hours(s),s)
        for s in ['','25:00-26:00','09:90-10:00','9:00-17:00','09:00-13:00,12:00-17:00','nan']:
            self.assertFalse(valid_hours(s),s)
    def test_duplicate_unknown_and_guards(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);targets=d/'targets.csv';pred=d/'pred.csv'
            targets.write_text('entity_id,date\nA,2026-12-24\n')
            header='entity_id,date,action,hours,confidence\n';line='A,2026-12-24,SUGGEST,closed,0.8\n'
            pred.write_text(header+line);self.assertTrue(validate(pred,targets)['valid'])
            pred.write_text(header+line+line)
            with self.assertRaises(ValueError):validate(pred,targets)
            pred.write_text(header+line.replace('A,','B,'))
            with self.assertRaises(ValueError):validate(pred,targets)
            pred.write_text(header+line)
            with self.assertRaises(ValueError):validate(pred,targets,{('A','2026-12-24'):{'shown':'closed','has_entry_now':0}})
            with self.assertRaises(ValueError):validate(pred,targets,{('A','2026-12-24'):{'shown':'09:00-17:00','has_entry_now':1}})
            pred.write_text(header+'A,2026-12-24,FLAG,,\n');self.assertTrue(validate(pred,targets)['valid'])

if __name__=='__main__':unittest.main()
