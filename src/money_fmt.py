"""Money formatting, shared. Integer cents in, human string out."""


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return "%s$%d.%02d" % (sign, c // 100, c % 100)
