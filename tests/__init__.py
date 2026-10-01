"""tests 包。

每个模块对应 core/ 或 server/ 下的一个模块，全部只用标准库 unittest，
不依赖网络（上游请求一律通过 monkeypatch 替换掉）。

运行全部：
    python3 -m unittest discover -s tests -t .
或者：
    python3 -m unittest discover -s tests
"""