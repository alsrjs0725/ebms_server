# 공유용 난이도표

이 폴더의 파일은 `/table/{경로}`로 로그인 없이 그대로 열립니다. 난이도표 하나를 `{표 이름}.html`로 두면
`https://<서버>/table/{표 이름}.html`로 공유할 수 있습니다.

beatoraja 등에서 난이도표로 등록하려면 HTML에 `<meta name="bmstable" content="{표 이름}/header.json">`을 넣고,
헤더 JSON의 `data_url`이 가리키는 데이터 JSON을 같은 폴더에 둡니다(형식은 `tables.py` 설명 참고).

```
table/
  {표 이름}.html
  {표 이름}/header.json
  {표 이름}/data.json
```

응답에는 다른 페이지와 같은 보안 헤더(CSP: 같은 사이트 스크립트·스타일만)와 `Access-Control-Allow-Origin: *`가 붙습니다.
