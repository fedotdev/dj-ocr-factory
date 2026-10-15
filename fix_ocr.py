import pathlib
import re
import pickle
import collections
import pymorphy3

MD = pathlib.Path("3-md_output/98-21118/full.md")
CACHE = pathlib.Path("C:/Users/fedor/AppData/Local/Temp/opencode/forms.pkl")

ALPH = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"

PROTECT = {
    "дб", "кгц", "мдб", "нб", "пб", "цб", "мрад", "мрах", "ап", "атс",
    "вдс", "миита", "цм", "юм", "жн", "оу", "гце", "це", "тре", "акульшин",
    "муравский", "сериализация", "километрическое", "километрического",
    "мкб", "двухпроводной", "двухпроводных", "двухпроводные", "двухпроводными",
    "двухпровожные", "однопроводная", "взаимозащищенность",
    "взаимозащищенности", "переприемными", "перечдоленным",
}


TRANS = str.maketrans("aocpexykmt", "аосрехукмт")


def norm(s):
    s = s.lower().replace("ё", "е")
    return s.translate(TRANS)


def load_forms():
    if CACHE.exists():
        return pickle.loads(CACHE.read_bytes())
    m = pymorphy3.MorphAnalyzer()
    forms = set()
    for p in m.iter_known_word_parses():
        forms.add(norm(p.word))
    CACHE.write_bytes(pickle.dumps(forms))
    return forms


def gen1(t):
    """все варианты на расстоянии 1"""
    res = set()
    for i in range(len(t)):
        for c in ALPH:
            res.add(t[:i] + c + t[i + 1:])
        res.add(t[:i] + t[i + 1:])
    for i in range(len(t) + 1):
        for c in ALPH:
            res.add(t[:i] + c + t[i:])
    for i in range(len(t) - 1):
        res.add(t[:i] + t[i + 1] + t[i] + t[i + 2:])
    return res


def best_fix(t, forms, counts):
    """t - нормализованный токен. вернуть исправление или None"""
    d1 = gen1(t).intersection(forms)
    hits = d1
    dist = 1
    if not hits and len(t) <= 12:
        d2 = set()
        for v in gen1(t):
            d2 |= gen1(v)
        hits = d2.intersection(forms)
        dist = 2
    if not hits:
        return None

    def score(w):
        return (
            0 if w in counts else 1,
            0 if dist == 1 else 1,
            -counts.get(w, 0),
        )

    scored = sorted((score(w), w) for w in hits)
    (s1, w1) = scored[0]
    if len(scored) == 1:
        return w1
    (s2, w2) = scored[1]
    if s1 < s2:
        return w1
    return None


def join_splits(md, forms):
    words = re.findall(r"[А-ЯЁа-яё]+", md)
    counts = collections.Counter(norm(w) for w in words)
    pat = re.compile(r"([А-ЯЁа-яё]{1,7}) ([А-ЯЁа-яё]{3,})")
    to_join = []
    for m in pat.finditer(md):
        a, b = m.group(1), m.group(2)
        joined = norm(a + b)
        if joined in forms or counts.get(joined, 0) >= 2:
            to_join.append(m.group(0))
    for t in sorted(set(to_join), key=len, reverse=True):
        md = md.replace(t, t.replace(" ", ""))
    return md


def join_hyphen(md):
    """кост-руктивных, неодно- родностями, перенос с дефисом"""
    return re.sub(r"([а-яё]{2,})-\s*([а-яё]{2,})", r"\1\2", md)


def join_uppercase(md):
    """С С С С Р -> СССР, но М П С С С С Р -> МПС СССР (известные аббревиатуры)"""
    md = re.sub(r"(?<=\b[А-ЯЁ])\s+(?=[А-ЯЁ]\b)", "", md)
    for a, b in [("МПССССР", "МПС СССР")]:
        md = md.replace(a, b)
    return md


def clean_formula_nums(md):
    """строки-номера формул с мусорным префиксом -> (номер)"""
    md = md.replace("（", "(").replace("）", ")")
    # \tag{,ДБ (2.6}, \tag{.ДБ (2.13} -> \tag{2.6}
    md = re.sub(
        r"\\tag\{[^()\n]*\(([IVXLC0-9 ]+\.[0-9]+[^()\n]*)[)}]", r"\\tag{\1}", md
    )
    # строки-номера: "，πB(I.4")", ", \text { ДБ } (I. 4 ^ {\prime})"
    pat = re.compile(r"^\s*[^()\n]*\(([^()\n]*)\)\s*$", re.M)
    return pat.sub(lambda m: f"({normalize_fnum(m.group(1))})", md)


def normalize_fnum(s):
    # I. 4 ^ {\prime} -> I.4', 2.7 ^ {\prime \prime} -> 2.7''
    s = s.replace(" ", "")
    s = s.replace(r"{\prime\prime}", "''").replace(r"{\prime}", "'")
    s = s.replace(r"^{\prime}", "'").replace(r"\prime", "'")
    return s.replace("^'", "'")


WORD = re.compile(r"[A-Za-zА-ЯЁа-яё]*[А-ЯЁа-яё][A-Za-zА-ЯЁа-яё]*")


def main():
    forms = load_forms()
    md = pathlib.Path(MD).read_text(encoding="utf-8")

    md = join_uppercase(md)
    md = join_hyphen(md)
    md = join_splits(md, forms)
    md = clean_formula_nums(md)

    words = WORD.findall(md)
    counts = collections.Counter(norm(w) for w in words)

    log, fixed = [], 0

    def fix_token(m):
        nonlocal fixed
        tok = m.group(0)
        t = norm(tok)
        if t in PROTECT or t in forms or len(t) < 4:
            return tok
        if tok.isupper() and len(tok) < 6:  # аббревиатура
            return tok
        f = best_fix(t, forms, counts)
        if not f:
            return tok
        fixed += 1
        if tok[0].isupper():
            f = f.capitalize()
        log.append(f"{tok!r} -> {f!r}")
        return f

    md = WORD.sub(fix_token, md)
    pathlib.Path(MD).write_text(md, encoding="utf-8")
    print(f"fixed: {fixed}")
    for l in log:
        print(l)


if __name__ == "__main__":
    main()
