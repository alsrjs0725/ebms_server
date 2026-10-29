"""BMS 패키지의 일본어·한국어 바이트(zip·7z 항목 이름, 차트 헤더)를 어느 인코딩으로 풀지 고릅니다.

cp932와 cp949는 서로의 바이트를 오류 없이 푸는 경우가 많아 decode 성공만으로는 고를 수 없으므로,
잘못 풀면 늘어나는 문자(odd_chars)가 가장 적은 인코딩을 씁니다. 맞는 인코딩이 없으면 대체하지 않고
(None, 이유들)을 돌려주며, 부르는 쪽이 오류로 처리합니다."""


def odd_chars(text: str) -> int:
    """잘못된 인코딩으로 풀면 늘어나는 문자 수. cp949 이름을 cp932로 풀면 반각 가타카나가,
    cp932 이름을 cp949로 풀면 KS X 1001 밖의 확장 한글(UHC)이 잔뜩 나옵니다."""
    count = 0
    for ch in text:
        if "｡" <= ch <= "ﾟ":
            count += 1
        elif "가" <= ch <= "힣":
            lead, trail = ch.encode("cp949")
            if lead < 0xB0 or trail < 0xA1:
                count += 1
    return count


def guess_encoding(raws: list[bytes], encodings: tuple[str, ...]) -> tuple[str | None, list[str]]:
    """바이트 전체를 오류 없이 풀 수 있는 인코딩 중 이상한 문자가 가장 적은 것과, 풀지 못한 인코딩의 이유.
    동점이면 encodings에서 앞선 것. 맞는 인코딩이 없으면 (None, 이유들)."""
    best = None
    failures = []
    for encoding in encodings:
        try:
            score = sum(odd_chars(raw.decode(encoding)) for raw in raws)
        except UnicodeDecodeError as e:
            failures.append(f"{encoding}: {e.object[max(0, e.start - 20):e.end + 20]!r}")
            continue
        if best is None or score < best[1]:
            best = (encoding, score)
    return (best[0] if best else None), failures
