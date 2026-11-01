"""示例代码：用于演示 CodeAgent 的审查 / 解释能力。

本文件刻意保留若干常见代码质量问题，便于观察离线审查引擎的输出。
"""


def process_data(items):
    result = []
    for it in items:
        try:
            value = it["value"]
            result.append(value * 2)
        except:
            pass
    return result


def very_long_function(a, b, c, d, e, f, g):
    total = a + b + c + d + e + f + g
    if total > 0:
        if total > 10:
            if total > 100:
                return total * 2
            else:
                return total
        else:
            return total
    else:
        return 0


password = "123456"
api_key = "sk-abcdef123456"


def main():
    data = [{"value": 1}, {"value": 2}, {"value": None}]
    print(process_data(data))


if __name__ == "__main__":
    main()
