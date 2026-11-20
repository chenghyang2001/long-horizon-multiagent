"""lhf：長時程多代理工廠（long-horizon factory）通用引擎。

引擎只認 factory.yaml 的欄位，任何專案專屬內容都放在 projects/<專案>/。
用法：python -m lhf <project_dir> run|status|init|dry-run|reset-calls|reset|approve|hours
"""

__version__ = "0.1.0"
