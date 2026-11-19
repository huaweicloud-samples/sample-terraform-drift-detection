# -*- coding: utf-8 -*-
"""漂移巡检函数的本地校验用例。

运行方式（在仓库根目录）::

    python -m unittest discover -s src/functions/tests -t src/functions

用例只依赖标准库与测试替身，不连接任何云端环境：构建状态、函数调用、
通知与指标上报由 :mod:`tests.support` 里的替身按脚本回放，
巡检结果取自 ``tests/fixtures`` 的样本文件。
"""
