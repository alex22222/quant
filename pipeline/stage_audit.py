# -*- coding: utf-8 -*-
"""阶段6·晋级审计：注册表↔门禁报告一致性自检自愈环路（策略库审查 P0）

每次 pipeline.run 自动执行：校验所有 live 策略的晋级证据
（策略代码/参数/门禁规则 hash、最新门禁判定），失配即自动降回 candidate。
"""
from .promote import audit


def main():
    return audit()
