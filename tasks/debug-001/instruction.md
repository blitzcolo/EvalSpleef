# 修复台账汇总工具

我们组用一个小工具把交易流水 CSV 汇总成月度报表（工作目录下的 **ledgerq/**，零第三方依赖，Python 3.11+）。最近财务对账时发现它有几处算得不对，但我没时间逐个复现，只能肯定不是输入文件的问题：同样的流水，人工核出来的数和工具出的数对不上，不止一处。

工具自己的 **README.md** 写了它应当遵守的行为契约（金额舍入、日期区间、输入校验、币种汇总、导出格式、库 API 兼容性）。请按这份契约把实现修好，让工具的行为重新符合 README 的承诺。

限制：

1. 不要修改 **tests/** 目录下的任何文件，也不要以修改测试的方式让测试通过——我们会在验收时核对测试文件哈希。
2. 不要引入第三方依赖，只用 Python 标准库（沙箱里没有网络）。
3. 保持 CLI 和库 API 不变（README 第 8 条），不要把命令行参数改名或删功能。
4. 尽量做小范围修复，不要重写整个工程；改完自己跑一遍：

       python3 -m unittest discover -s tests -v
       python3 -m ledgerq summary --input sample/transactions.csv --output /tmp/summary.json
       python3 -m ledgerq export  --input sample/transactions.csv --output /tmp/transactions.csv

完成后说明你改了什么、为什么这么改。
