import numpy as np
from common import ivs

def span(s):
    iv = ivs(s)
    return (iv[0][0], iv[-1][1]) if iv else (None, None)

def classify(T, R):
    if T == 'closed':
        return ('CLOSED_REGCLOSED' if R == 'closed' else 'CLOSED', np.nan, np.nan)
    if R == 'closed':
        return ('OPEN_ON_CLOSED', np.nan, np.nan)
    if T == R:
        return ('REGULAR', 0, 0)
    ot, ct = span(T)
    orr, cr = span(R)
    do, dc = (ot - orr, ct - cr)
    if do == 0 and dc == 0:
        c = 'SAMESPAN_OTHER'
    elif do == 0 and dc < 0:
        c = 'SHORT_CLOSE'
    elif do > 0 and dc == 0:
        c = 'SHORT_OPEN'
    elif do > 0 and dc < 0:
        c = 'SHORT_BOTH'
    elif do <= 0 and dc >= 0:
        c = 'LONGER'
    else:
        c = 'SHIFTED'
    return (c, do, dc)

def fmt(m):
    m = int(m) % 1440
    return f'{m // 60:02d}:{m % 60:02d}'

def to_str(iv):
    if not iv:
        return 'closed'
    return ','.join((f'{fmt(a)}-{fmt(b)}' for a, b in iv))

def encode(cls, H, reg):
    if cls == 'CLOSED':
        return ('closed',)
    if cls == 'CLOSED_REGCLOSED':
        return ('closed_or_regular',)
    if cls == 'REGULAR':
        return ('regular',)
    if cls == 'SHORT_CLOSE':
        return ('trunc', ivs(H)[-1][1])
    if cls == 'LONGER':
        t, r = (ivs(H), ivs(reg))
        return ('extend', t[0][0] - r[0][0], t[-1][1] - r[-1][1])
    return ('fixed', H)

def apply(h, reg, ambiguous='closed'):
    k = h[0]
    if k == 'closed':
        return 'closed'
    if k == 'closed_or_regular':
        return 'closed' if ambiguous == 'closed' else reg
    if k == 'regular':
        return reg
    if k == 'fixed':
        return h[1]
    r = ivs(reg)
    if not r:
        return 'closed' if k == 'trunc' else 'closed'
    if k == 'trunc':
        X = h[1]
        out = [(a, min(b, X)) for a, b in r if a < X]
        return to_str(out) if out else 'closed'
    if k == 'extend':
        r = list(r)
        r[0] = (r[0][0] + h[1], r[0][1])
        r[-1] = (r[-1][0], r[-1][1] + h[2])
        return to_str(r)
    raise ValueError(h)

def key(h):
    return '|'.join((str(x) for x in h))
