# 从 Python 到你的第一个 Agent

> 为你的两轮回答编写 · Python 3.13 · 标准库实现 · 初稿 2026-10-05，实测 2026-10-06

## 摘要

这本教材带你亲手连接一条完整的数据流：**Python 组织消息 → 通过 HTTP 发送给模型 → 模型提出工具调用 → Python 执行函数 → 把结果加入消息 → 再次请求模型 → 得到最终回答。**

你已经知道上下文、工具调用和 ReAct 的大意，也能区分“函数本身”和“调用函数得到的值”。因此，我们不从术语堆砌开始，而从几段能观察到变化的 Python 程序开始，补齐你在回答中混淆的地方：列表的层级、修改对象与函数返回值、接口字段与自己起的变量名。

最终成品是一个有 `add` 和 `multiply` 两个工具的计算 Agent。它能先调用加法得到 42，再把工具结果交给模型，由模型提出乘法调用得到 294，最后形成文字回答。前期示例完全离线；联网部分才使用你已经提供的 DeepSeek 密钥。

这里的学习目标不是记住一整段代码，而是能指着其中一行说清楚：**这行读了什么数据、改变了什么数据、下一步谁会使用它。**

## 目录

- [开始前：运行方法与阅读方法](#start)
- [第 1 章：看懂消息的数据结构](#chapter1)
- [第 2 章：看懂程序怎样改变数据](#chapter2)
- [第 3 章：字典怎样变成可发送的数据](#chapter3)
- [第 4 章：第一次调用 DeepSeek](#chapter4)
- [第 5 章：让模型接上前面的对话](#chapter5)
- [第 6 章：先在本地模拟工具调用](#chapter6)
- [第 7 章：让模型真正提出工具调用](#chapter7)
- [第 8 章：把工具结果送回模型](#chapter8)
- [第 9 章：搭建完整 Agent 循环](#chapter9)
- [第 10 章：停止与排错](#chapter10)
- [课后习题汇总](#exercises)
- [常见误区速查](#mistakes)
- [参考答案](#answers)
- [选学附录：保存历史、finish、流式输出](#optional)
- [官方资料与验证说明](#sources)

<a id="start"></a>
## 开始前：运行方法与阅读方法

### 先把程序运行起来

已经确认工作区的 `.venv` 使用 Python 3.13.7。这些程序只用 Python 标准库，不需要安装 SDK 或其他依赖。

在 PowerShell 中执行：

```powershell
Set-Location -LiteralPath '<工作区>'
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\01_messages.py'
```

第一行切换工作目录；第二行运行脚本。`&` 是 PowerShell 用来执行引号里程序路径的符号，不是 Python 语法。`-X utf8` 让 Python 使用 UTF-8 模式，减少中文输出的编码问题。后面各章沿用这条命令，只替换文件名。在 PyCharm 中也可以选择这个 `.venv` 解释器，再运行对应文件。

本书中的 `>>>`、输出文字、PowerShell 命令都不要当作 Python 代码抄进 `.py` 文件。代码框上方会说明它是程序、输出还是局部片段。

### 阅读节奏

每章只做三件事：先猜一小段输出，运行后对照，再改一个地方。遇到预测错误，回到数据变化那一步，不必重学整份 Python 笔记。

新语法第一次出现时会解释。完整脚本都有对应文件；正文中的局部片段用来放大某一步，不要求把所有代码框依次拼起来。文末提供练习答案，先尝试再查看即可，不需要向我提交答案才能往下学。

### 文件与费用

| 文件 | 是否联网 | 一次运行的调用次数 |
|---|---|---:|
| `01_messages.py`、`02_change_and_return.py`、`03_json_bytes.py` | 否 | 0 |
| `04_first_api.py` | 是 | 1 |
| `api_client.py` | 仅定义函数，单独运行不联网 | 0 |
| `05_history.py` | 是 | 2 |
| `06_local_tools.py` | 否 | 0 |
| `07_tool_request.py` | 是 | 1 |
| `08_tool_result.py` | 是 | 2 |
| `09_mini_agent.py` | 是 | 最多 5 |
| `10_errors_and_limits.py`、`11_save_history.py`、`12_finish_demo.py` | 否 | 0 |
| `13_stream_text.py` | 是 | 1 |

联网示例每次最多生成 512 tokens，不自动重试。tokens 是模型计算文本用量的单位，不等同于中文字数。你多次手动运行就会多次计费；五次上限限制的是单个 Agent 任务的请求次数，不是整个账户的金额。编写期间的实际验证用量见同目录的 `验证记录.md`。

程序从工作区已有的 `DeepSeekKey（UTF8）.txt` 读取密钥。该文件应只包含密钥文本；程序不会打印它。教材和示例里只出现文件名，不出现真实密钥。

<a id="chapter1"></a>
## 第 1 章：看懂消息的数据结构

### 具体问题：一条消息和一个消息列表有什么区别？

你已经会用 `message["content"]` 取值。之前“没学过数据结构”的回答主要是术语不熟悉，不代表你没学过列表和字典。这里把它们连接起来。

先看最小代码：

```python
message = {"role": "user", "content": "你好"}
messages = [message]
```

`message` 是一个字典，里面有两个键。`messages` 是一个列表，里面目前只有一个元素；这个元素恰好是那个字典。变量名有没有 `s` 不会改变类型，决定类型的是等号右边创建了什么对象。

完整程序：[01_messages.py](示例程序/01_messages.py)

```python
"""第 1 章：列表装消息，字典描述一条消息。离线运行。"""

message = {"role": "user", "content": "你好"}
messages = [message]

print("单条消息的正文：", message["content"])
print("列表的第一个元素：", messages[0])
print("第一个元素的正文：", messages[0]["content"])
print("列表的元素个数：", len(messages))
print("第一个字典的键数：", len(messages[0]))

# 同名键只保留最后一次赋予的值。
example = {"role": "user", "role": "assistant"}
print("重复键的结果：", example)

messages = [
    {"role": "user", "content": "我叫小林"},
    {"role": "user", "content": "我叫什么名字？"},
]
print("现在有几条消息：", len(messages))
print("上一条消息：", messages[-2]["content"])
print("当前这条消息：", messages[-1]["content"])
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\01_messages.py'
```

```text
单条消息的正文： 你好
列表的第一个元素： {'role': 'user', 'content': '你好'}
第一个元素的正文： 你好
列表的元素个数： 1
第一个字典的键数： 2
重复键的结果： {'role': 'assistant'}
现在有几条消息： 2
上一条消息： 我叫小林
当前这条消息： 我叫什么名字？
```

### 逐段解释：每次只走一层

| 表达式 | 得到什么 | 类型 |
|---|---|---|
| `messages` | 装着一条消息的列表 | `list` |
| `messages[0]` | 第一条消息 | `dict` |
| `messages[0]["content"]` | 第一条消息的正文 | `str` |

`[0]` 在列表中取第一个元素，`["content"]` 再从该字典按键取值。你不必一次看懂很长的索引链，可以先写临时变量：

```python
first_message = messages[0]
content = first_message["content"]
```

`len()` 只数当前这一层。因此 `len(messages)` 数的是消息条数，`len(messages[0])` 数的是第一条消息的键数。它不会自动深入，把所有键和值加起来。

再看重复键：`{"role": "user", "role": "assistant"}` 最后只剩 `{"role": "assistant"}`，后面的值覆盖了前面的值。换行和缩进只是排版，不能让一个字典自动变成两个字典。两条消息需要两个 `{...}`，并作为两个元素放进外面的 `[...]`。

你原来那份代码的最外层列表只有 **1** 个元素；内部字典有 `role`、`history content`、`content` **3** 个不同的键，重复的 `role` 不会另占一个键。

### 常见误区

`messages[0][“conrent”]` 的取值思路是对的，但代码有两个独立问题：中文弯引号不是 Python 的字符串定界符；`conrent` 与 `content` 拼写不同。应写成 `messages[0]["content"]`。

列表常被口头称为“数组”，本书统一叫“列表”，避免和你学过的 NumPy 数组混淆。

### 课后习题

- **1A 预测：**`messages = [{"role": "user", "content": "A"}, {"role": "assistant", "content": "B"}]`，`len(messages)` 和 `len(messages[0])` 各是多少？
- **1B 修改：**追加第三条用户消息“继续”，打印第三条消息的正文。
- **1C 补写：**对 `response = {"choices": [{"message": {"content": "你好"}}]}`，分三步取出“你好”，再写成一行。

<a id="chapter2"></a>
## 第 2 章：看懂程序怎样改变数据

### 具体问题：外面的列表为什么变了，result 为什么却是 None？

先看最小代码：

```python
messages = []
value = messages.append("你好")
```

等号右边先执行。`append` 把“你好”加入原列表，同时它这次调用的返回值是 `None`。随后 `value` 接住的是 `None`，不是刚加入的元素。`None` 是 Python 中表示“没有具体值”的对象，不是字符串 `"None"`，也不是空列表。

完整程序：[02_change_and_return.py](示例程序/02_change_and_return.py)

```python
"""第 2 章：修改列表和返回一个值是两件事。离线运行。"""

messages = []
value = messages.append({"role": "user", "content": "你好"})
print("append 后的列表：", messages)
print("append 的返回值：", value)


def change_a(items):
    items.append("你好")


messages = []
result = change_a(messages)
print("A 的列表：", messages)
print("A 的返回值：", result)


def change_b(items):
    return "你好"


messages = []
result = change_b(messages)
print("B 的列表：", messages)
print("B 的返回值：", result)


def change_c(items):
    items.append("你好")
    return len(items)


messages = []
result = change_c(messages)
print("C 的列表：", messages)
print("C 的返回值：", result)
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\02_change_and_return.py'
```

```text
append 后的列表： [{'role': 'user', 'content': '你好'}]
append 的返回值： None
A 的列表： ['你好']
A 的返回值： None
B 的列表： []
B 的返回值： 你好
C 的列表： ['你好']
C 的返回值： 1
```

### 逐段解释：分开追踪两件事

调用 `change_a(messages)` 时，函数里的名字 `items` 与外面的名字 `messages` 指向同一个列表。`items.append("你好")` 修改的就是这个列表。

函数 A 没有写 `return`，执行到末尾会返回 `None`。函数 B 写了 `return "你好"`，但没有修改列表。函数 C 两件事都做了：先修改列表，再返回列表的长度。

| 程序 | 对列表做了什么 | 函数返回什么 | 外面的 `messages` | 外面的 `result` |
|---|---|---|---|---|
| A | 加入“你好” | 没写 `return`，返回 `None` | `["你好"]` | `None` |
| B | 没有修改 | 返回字符串“你好” | `[]` | `"你好"` |
| C | 加入“你好” | 返回长度 | `["你好"]` | `1` |

你的预测把这两个通道混在了一起。以后遇到函数，分别问：**它修改了什么对象？它通过 return 交回了什么值？** 这两个问题可以有不同答案。

还有一个边界：如果函数里写 `items = []`，只是让局部名字 `items` 指向另一个列表，不会把外面的 `messages` 自动重新绑定。这与 `items.append(...)` 修改同一个列表不同。

### 常见误区

`return` 不会自动把参数修改掉，也不会自动返回函数中最后创建的字典。`messages = messages.append(...)` 会先修改旧列表，再把变量 `messages` 绑定为 `None`；下一次写 `messages.append(...)` 就会失败。

对 Agent 来说，通常要写两行：先 `assistant_message = ask_llm(messages)` 接住模型返回的消息，再 `messages.append(assistant_message)` 把它加入历史。

### 课后习题

- **2A 预测：**`items = []; result = items.append(7)`，`items` 和 `result` 分别是什么？
- **2B 修改：**修改 `change_a`，让它修改列表后，返回字符串“已添加”。
- **2C 补写：**写 `add_user_message(messages, text)`，把一条用户消息加入列表，并返回添加后的消息条数。

<a id="chapter3"></a>
## 第 3 章：字典怎样变成可发送的数据

### 具体问题：Python 字典怎么交给远端服务器？

字典是当前 Python 程序内存里的对象。HTTP 发送的是字节，双方需要约定字节表达的内容。本课程的接口使用 JSON：它是一种表示数据的文本格式，不是 Python 字典本身。

本章首次使用 `import json`：把 Python 标准库中的 `json` 模块加载进来，随后可以通过 `json.dumps` 等名字调用其功能。无需自己编写 JSON 转换器，也无需安装这个模块。

最小转换链是：

```python
text = json.dumps(data, ensure_ascii=False)
body = text.encode("utf-8")
```

完整程序：[03_json_bytes.py](示例程序/03_json_bytes.py)

```python
"""第 3 章：字典、JSON 文本、字节之间的转换。离线运行。"""

import json

data = {"role": "user", "content": "你好"}
text = json.dumps(data, ensure_ascii=False)
body = text.encode("utf-8")

print("data 的类型：", type(data))
print("text 的类型：", type(text))
print("body 的类型：", type(body))
print("JSON 文本：", text)
print("编码后的字节：", body)

# 假设 body 是刚刚从网络接收到的字节。
received_text = body.decode("utf-8")
received_data = json.loads(received_text)
print("恢复出的字典：", received_data)
print("恢复出的正文：", received_data["content"])
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\03_json_bytes.py'
```

```text
data 的类型： <class 'dict'>
text 的类型： <class 'str'>
body 的类型： <class 'bytes'>
JSON 文本： {"role": "user", "content": "你好"}
编码后的字节： b'{"role": "user", "content": "\xe4\xbd\xa0\xe5\xa5\xbd"}'
恢复出的字典： {'role': 'user', 'content': '你好'}
恢复出的正文： 你好
```

### 逐段解释：四个动作各做一件事

| 动作 | 输入 | 输出 |
|---|---|---|
| `json.dumps(data)` | Python 数据 | JSON 字符串 |
| `text.encode("utf-8")` | 字符串 | 字节 `bytes` |
| `body.decode("utf-8")` | 字节 | 字符串 |
| `json.loads(text)` | JSON 字符串 | Python 数据 |

`type(x)` 用来观察对象的类型。`ensure_ascii=False` 让输出文本中的中文保持可读；不设置时，中文可能显示为 `\u...` 转义，仍然可以正确解析。编码选项影响文本表示，不改变“你好”的含义。

`dumps` 中的 `s` 可以帮助你记成 string，表示转成字符串；`loads` 表示从字符串解析。不要用 `str(data)` 代替 JSON 编码：Python 的单引号、`True`、`None` 等表示法并不等于 JSON 的语法。JSON 使用双引号、`true` 和 `null`。

### 常见误区

看起来像字典的字符串，仍然是字符串。例如 `'{"a": 2}'` 不能直接当字典用 `text["a"]` 取值，需要先 `json.loads(text)`。以后工具调用里的 `arguments` 正好会遇到这种情况。

Python 的 `None`、布尔值、列表、字典等可以转换为相应 JSON 数据；Python 函数不能直接用默认的 `json.dumps` 转成服务器可执行的函数。工具代码留在本地，发送的是它的说明。

### 课后习题

- **3A 预测：**`text = '{"a": 2}'`，`type(text)` 是什么？执行 `json.loads(text)` 后是什么？
- **3B 修改：**把示例正文改成“我叫小林”，验证转换回来后仍然相同。
- **3C 补写：**给定 `data = {"a": 2}`，写出得到 `body` 再恢复 `restored` 的四个转换步骤。

<a id="chapter4"></a>
## 第 4 章：第一次调用 DeepSeek

### 具体问题：从空白文件开始，第一行写什么？

先不急着写网络调用。把任务分成五步：**准备消息、读取密钥、构建请求数据、发送字节、解析响应。** 第一行通常只是导入接下来要用的模块，不需要一开始就写出整套系统。

| HTTP 部分 | 这次负责什么 |
|---|---|
| URL | 请求送到 `https://api.deepseek.com/chat/completions` |
| Method | `POST`：向这个接口提交数据 |
| Headers | 描述正文格式，并通过 `Authorization` 携带鉴权信息 |
| Body | 模型名、消息列表以及生成选项，编码成 JSON 字节 |
| Response | HTTP 状态及响应正文；成功时正文里包含模型消息 |

`API` 是服务提供的接口，`API key` 是访问凭证。你之前把 API 口头简称为“钥匙”，编程时要把这两个名字分开。

新语法只补这几项：`from pathlib import Path` 导入一个处理路径的工具；`__file__` 是当前脚本文件的路径；`Path(...).resolve()` 得到绝对路径；`parents[0]` 是父目录，`parents[1]` 是再上一层，`parents[2]` 是工作区。这里路径中的 `/` 表示拼接路径，不是除法。

`read_text(...)` 读取文件成字符串，`strip()` 去除首尾换行。`utf-8-sig` 兼容带 UTF-8 BOM 的文本。路径定位与读取密钥是准备工作，密钥不会放进消息正文。

完整程序：[04_first_api.py](示例程序/04_first_api.py)

```python
"""第 4 章：完整展开一次 HTTP 请求。运行会调用付费 API。"""

import json
import urllib.request
from pathlib import Path

# 当前文件在 工作区/教材/示例程序 中，parents[2] 是工作区。
workspace = Path(__file__).resolve().parents[2]
key_file = workspace / "DeepSeekKey（UTF8）.txt"
api_key = key_file.read_text(encoding="utf-8-sig").strip()

messages = [{"role": "user", "content": "请只回复：你好，小林。"}]
data = {
    "model": "deepseek-flash",
    "messages": messages,
    "stream": False,
    "thinking": {"type": "disabled"},
    "max_tokens": 512,
}
body = json.dumps(data, ensure_ascii=False).encode("utf-8")

request = urllib.request.Request(
    "https://api.deepseek.com/chat/completions",
    data=body,
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    },
    method="POST",
)

print("发送的消息：", messages)
with urllib.request.urlopen(request, timeout=30) as response:
    response_bytes = response.read()

response_text = response_bytes.decode("utf-8")
response_data = json.loads(response_text)
choice = response_data["choices"][0]
if choice["finish_reason"] != "stop":
    raise RuntimeError("本次回答未正常完成：" + choice["finish_reason"])
assistant_message = choice["message"]
answer = assistant_message["content"]
print("回答：", answer)
print("本次用量：", response_data.get("usage", {}))
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\04_first_api.py'
```

一次成功运行的输出形状如下；回答措辞和 token 数量可能不同：

```text
发送的消息： [{'role': 'user', 'content': '请只回复：你好，小林。'}]
回答： 你好，小林。
本次用量： {...token 统计字段...}
```

### 逐段解释：请求对象不等于已经发送

`data` 是外层字典，`messages` 只是其中一个字段。因此请求正文不仅是“我向 AI 问的问题”，还包含模型和其他选项。`stream=False` 要求返回完整响应，`thinking={"type": "disabled"}` 使用非思考模式，`max_tokens=512` 限制输出用量。这些是当前 DeepSeek 接口的约定，不是所有服务通用的 Python 语法。

`urllib.request.Request(...)` 只构建请求对象。真正访问远端的是 `urllib.request.urlopen(request, timeout=30)`。`timeout` 用来限制网络操作等待，不代表模型一定能在 30 秒内完成整个任务。

`with ... as response` 在这个代码块结束时关闭网络响应资源。`response.read()` 读到字节，然后依次解码和解析成字典。

返回值沿着下面的层级取出来：

```text
response_data（字典）
└─ choices（列表）
   └─ 第 0 个元素（字典）
      ├─ finish_reason（字符串）
      └─ message（字典）
         └─ content（字符串）
```

本章把这条链拆成 `choice`、`assistant_message`、`answer` 三个变量，你也可以写成 `response_data["choices"][0]["message"]["content"]`。能解释每一层比记住长表达式更有用。

`raise RuntimeError(...)` 主动报告异常；它不是网络发送指令。这里检查 `finish_reason`，防止把因为长度限制而截断的内容误当完整答案。`response_data.get("usage", {})` 取统计信息，没有该键时返回空字典。

### 常见误区

构建了 `Request` 不等于请求已送出；把 `body` 打印出来也不等于发送。鉴权失败通常会收到 HTTP 错误响应，`urllib` 会抛出异常，并不总是“服务器不回应”。暂时看到异常没关系，第 10 章会教你按失败阶段定位。

### 课后习题

- **4A 预测：**如果只运行到 `request = urllib.request.Request(...)` 就结束程序，会产生模型调用吗？
- **4B 修改：**把用户正文改成“用一句话解释列表”，运行一次，观察回答。
- **4C 补写：**用五行文字说明“字典 → 请求字节 → 网络响应 → 字典 → 回答”的步骤，并指出哪行真正联网。

<a id="chapter5"></a>
## 第 5 章：让模型接上前面的对话

### 具体问题：第二次请求怎么带上第一次说的话？

你已经知道要发送历史。现在把这个判断落到代码里：消息仍然放在同一个列表中，每条消息仍然使用 `role` 和 `content`。历史是由列表中的位置和角色表示的，不是把键改成 `history_content`。

先把上一章重复的 HTTP 代码放入函数。`def ask_llm(messages, tools=None, tool_choice=None)` 定义三个参数，后两个默认是 `None`，当前可以不传。进入工具章节时再传入。

完整请求函数：[api_client.py](示例程序/api_client.py)

```python
"""第 5 章起复用：把第 4 章的 HTTP 过程放入一个函数。"""

import json
import urllib.request
from pathlib import Path


def ask_llm(messages, tools=None, tool_choice=None):
    workspace = Path(__file__).resolve().parents[2]
    key_file = workspace / "DeepSeekKey（UTF8）.txt"
    api_key = key_file.read_text(encoding="utf-8-sig").strip()

    data = {
        "model": "deepseek-flash",
        "messages": messages,
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 512,
    }
    if tools is not None:
        data["tools"] = tools
    if tool_choice is not None:
        data["tool_choice"] = tool_choice

    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response_data = json.loads(response.read().decode("utf-8"))

    print("本次用量：", response_data.get("usage", {}))
    choice = response_data["choices"][0]
    if choice["finish_reason"] not in ("stop", "tool_calls"):
        raise RuntimeError("本次生成未正常完成：" + choice["finish_reason"])
    return choice["message"]
```

### 这个函数的输入和输出

输入的 `messages` 是列表，返回的是 `choice["message"]` 这个**完整的助手消息字典**。这里的 `return` 不会自动把消息放入输入列表。

`if tools is not None` 表示调用者确实提供了工具列表，才把它加入请求。现在先把这两段当作后面章节要用的插口。`finish_reason` 可以是正常文字结束 `stop`，也可以是等待工具执行的 `tool_calls`；其他情况报告失败。

然后在同目录的另一个文件使用 `from api_client import ask_llm`，意思是从 `api_client.py` 里取出已经定义好的函数。导入该文件只定义函数，不会自动发送请求。

完整对话程序：[05_history.py](示例程序/05_history.py)

```python
"""第 5 章：保存消息，并在第二次请求时再次发送。调用两次 API。"""

from api_client import ask_llm

messages = [{"role": "system", "content": "请用一句简短的话回答。"}]

messages.append({"role": "user", "content": "我叫小林。"})
print("第一次发送：", messages)
assistant_message = ask_llm(messages)
print("第一次回答：", assistant_message["content"])
messages.append(assistant_message)

messages.append({"role": "user", "content": "我叫什么名字？"})
print("第二次发送：", messages)
assistant_message = ask_llm(messages)
print("第二次回答：", assistant_message["content"])
messages.append(assistant_message)
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\05_history.py'
```

示意输出：

```text
第一次发送： [system 消息, 用户说“我叫小林”的消息]
本次用量： {...}
第一次回答： 你好，小林。
第二次发送： [system 消息, 用户说“我叫小林”的消息, 助手回答, 用户的新问题]
本次用量： {...}
第二次回答： 你叫小林。
```

方括号中的描述只是便于阅读的示意，实际会打印完整字典列表。

### 逐段解释：每一轮多了什么？

| 时刻 | 列表里的角色顺序 | 本地动作 |
|---|---|---|
| 第一次请求前 | `system, user` | 加入“我叫小林” |
| 第一次请求后 | `system, user, assistant` | 加入模型返回的消息 |
| 第二次请求前 | `system, user, assistant, user` | 加入“我叫什么名字” |
| 第二次请求后 | `system, user, assistant, user, assistant` | 加入第二次回答 |

`system` 在这里放固定说明；`user` 放用户说的话；`assistant` 放模型回复。当前使用的 Chat Completions 调用依靠本次发送的消息来获得上下文，不能假定它自动找回你先前的独立请求。

“上下文注入”在这个例子里，就是把相关信息写进 `messages`，然后让请求正文携带这个列表。以后加入检索到的资料，也需要落实为模型本次能收到的数据。

### 常见误区

`messages[-2]["content"] = "history_content"` 会把那条消息的正文替换成字面字符串 `history_content`，不会把它标记成历史。`{"role": user}` 读取的是变量 `user`；如果想写角色名称，应写 `{"role": "user"}`。

把历史写进磁盘也不等于模型已经看到了它；还需要读取、组织并随请求发送。当前例子只在程序运行期间保存列表，下次启动会重新创建它。

### 课后习题

- **5A 预测：**第二次请求发送前，`len(messages)` 是多少？
- **5B 修改：**增加第三轮“请把我的名字重复两遍”，记得保存助手回复。
- **5C 补写：**写三行核心代码：追加用户消息、请求模型、追加助手消息。

<a id="chapter6"></a>
## 第 6 章：先在本地模拟工具调用

### 具体问题：只有一个工具名称，怎么找到函数？

你正确理解了 `{"add": add}` 保存函数本身，`{"add": add(2, 3)}` 保存计算结果。我们沿着这个理解继续，先用手写字典代替模型决定。

完整程序：[06_local_tools.py](示例程序/06_local_tools.py)

```python
"""第 6 章：把模型的决定暂时用一份手写数据代替。离线运行。"""


def add(a, b):
    return a + b


def multiply(a, b):
    return a * b


tools = {"add": add, "multiply": multiply}
decision = {"name": "add", "arguments": {"a": 23, "b": 19}}

name = decision["name"]
arguments = decision["arguments"]
selected_tool = tools[name]
a = arguments["a"]
b = arguments["b"]
result = selected_tool(a, b)

print("工具名称：", name)
print("参数：", arguments)
print("结果：", result)
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\06_local_tools.py'
```

```text
工具名称： add
参数： {'a': 23, 'b': 19}
结果： 42
```

### 逐段解释：先找函数，再准备参数，最后调用

`decision` 只是数据。`tools[name]` 按名字从字典拿到函数，并没有执行。`arguments["a"]`、`arguments["b"]` 分别取得数值。直到 `selected_tool(a, b)` 才真正运行加法。

这几行就是一个最简单的工具分发过程。所谓 registry 是这个名称到函数的字典；所谓 dispatcher 是查找函数并把参数交给它的代码，不需要额外的框架。

你写的 `add(arguments[a], arguments[b])` 已经接近正确。这里 `a`、`b` 是字典中的字符串键，所以需要引号：

```python
result = add(arguments["a"], arguments["b"])
```

对于 `def add(a, b)`，直接 `add(arguments)` 只传了一个位置参数，还缺 `b`。Python 函数当然能接收字典；如果定义的是 `def read_argument(arguments)`，它就可以接收整个字典。问题在于调用方式要与函数定义对应。

理解显式取值后，可以认识一种简写：`add(**arguments)` 把字典展开成按名字传参，等同于这里的 `add(a=23, b=19)`。主线代码继续使用显式取值，方便你看清数据去哪了。

### 常见误区

工具名称是字符串，函数是可调用对象，两者通过本地字典对应。不要把模型输出当作 Python 代码交给 `eval` 或 `exec`；本课程只执行自己明确放进工具字典的函数。

### 课后习题

- **6A 预测：**构建 `tools = {"add": add}` 时，加法执行了吗？
- **6B 修改：**把决定改成调用 `multiply`，参数为 6 和 7。
- **6C 补写：**新增 `subtract(a, b)`，注册到字典并通过名称调用，计算 10 减 3。

<a id="chapter7"></a>
## 第 7 章：让模型真正提出工具调用

### 具体问题：模型怎么知道工具叫什么、需要哪些参数？

Python 函数在本地，远端模型看不到你的变量和函数定义。你要发送一份工具描述，写明名称、用途和参数格式。

| 部分 | 在哪里 | 用途 |
|---|---|---|
| `def add(a, b)` | 本地 Python | 真正计算 |
| `{"add": add}` | 本地 Python | 根据名称找到函数 |
| `tool_definitions` | 通过请求发送 | 告诉模型有哪些工具可选 |

schema 可以理解为“数据应该长什么样的说明”。这里 `object` 对应参数对象，`properties` 描述其中的字段，`number` 要求数值，`required` 列出必须提供的字段。这些英文键是接口规范的一部分。

完整程序：[07_tool_request.py](示例程序/07_tool_request.py)

```python
"""第 7 章：只观察模型提出的调用，不执行工具。调用一次 API。"""

import json
from api_client import ask_llm

tool_definitions = [
    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "计算两个数字的和。",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "第一个数"},
                    "b": {"type": "number", "description": "第二个数"},
                },
                "required": ["a", "b"],
            },
        },
    }
]

messages = [{"role": "user", "content": "请调用 add 计算 23 加 19。"}]
# 本节为了稳定观察格式，要求模型使用 add。
choice = {"type": "function", "function": {"name": "add"}}
message = ask_llm(messages, tools=tool_definitions, tool_choice=choice)
print("模型的完整消息：", message)

tool_call = message["tool_calls"][0]
arguments_text = tool_call["function"]["arguments"]
arguments = json.loads(arguments_text)
print("调用 ID：", tool_call["id"])
print("工具名称：", tool_call["function"]["name"])
print("参数原本的类型：", type(arguments_text))
print("解析后的参数：", arguments)
print("到这里仍未执行加法。")
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\07_tool_request.py'
```

响应形状示意：

```python
{
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "某个调用ID",
            "type": "function",
            "function": {
                "name": "add",
                "arguments": '{"a":23,"b":19}'
            }
        }
    ]
}
```

实际 ID、空白和额外字段可能不同。`content` 为 `None` 或空字符串不代表失败：此时助手给出的是工具请求。它也可能一边说“我来计算”，一边给出 `tool_calls`。因此，不能仅凭 `content` 有文字就认定任务结束，应先检查是否有工具调用。

### 逐段解释：这里出现两次 JSON 解析

第一次在 `ask_llm()` 里，把 HTTP 响应的 JSON 文本解析成外层 Python 字典。接口约定工具参数 `arguments` 本身仍是一个 JSON 字符串，所以还要做第二次：

```python
arguments_text = tool_call["function"]["arguments"]
arguments = json.loads(arguments_text)
```

之后才可以取 `arguments["a"]`。并不是所有嵌套字典都要反复 `loads`；只对仍然是 JSON 字符串的那一层解析。

这节用 `tool_choice` 指定 `add`，目的是稳定观察协议。第 9 章不指定工具，让模型依据问题和工具结果选择下一步。即使模型已经给出参数，到本节最后仍没有任何一行执行 `add`。

### 常见误区

工具说明会引导模型生成参数，但不能代替本地参数检查。`"required": ["a", "b"]` 是发给模型/API 的约定；真正调用 Python 函数前仍可能遇到缺字段或类型不合适。

第 6 章的 `{"name": ..., "arguments": ...}` 是本地简化格式，本章才使用真实接口返回的 `tool_calls` 结构。不要把两种格式混抄。

### 课后习题

- **7A 预测：**上面的 `arguments` 字段，在执行第二次 `json.loads` 前是什么类型？
- **7B 修改：**在工具描述中更明确地说明 `a` 和 `b` 是哪两个数；观察这是改了说明还是改了计算代码。
- **7C 补写：**照着加法的格式写出乘法工具定义，名字叫 `multiply`。

<a id="chapter8"></a>
## 第 8 章：把工具结果送回模型

### 具体问题：Python 算出了 42，远端模型怎么知道？

本地变量 `result` 不会自动出现在模型的上下文中。需要保存助手提出的调用消息，再添加与这个调用对应的工具结果，然后发送下一次请求。

完整程序：[08_tool_result.py](示例程序/08_tool_result.py)

```python
"""第 8 章：完整执行一次工具，再把结果交给模型。调用两次 API。"""

import json
from api_client import ask_llm


def add(a, b):
    return a + b


tool_definitions = [
    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "计算两个数字的和。",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
        },
    }
]
messages = [{"role": "user", "content": "请调用 add 计算 23 加 19，再告诉我结果。"}]
choice = {"type": "function", "function": {"name": "add"}}
message = ask_llm(messages, tools=tool_definitions, tool_choice=choice)
messages.append(message)

# 一条助手消息可能包含多个调用，因此逐个处理。
for tool_call in message["tool_calls"]:
    name = tool_call["function"]["name"]
    if name != "add":
        raise ValueError("本节只实现了 add 工具")
    arguments = json.loads(tool_call["function"]["arguments"])
    result = add(arguments["a"], arguments["b"])
    print("实际执行：", name, arguments, "得到：", result)
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "content": str(result),
    })

print("送回模型的消息：", messages)
# 这一节只有一次工具阶段，第二次请求明确要求给出文字回答。
final_message = ask_llm(messages, tools=tool_definitions, tool_choice="none")
messages.append(final_message)
print("最终回答：", final_message["content"])
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\08_tool_result.py'
```

```text
本次用量： {...}
实际执行： add {'a': 23, 'b': 19} 得到： 42
送回模型的消息： [用户问题, 助手提出的工具调用, 对应的工具结果]
本次用量： {...}
最终回答： 23 加 19 的结果是 42。
```

### 逐段解释：一次调用对应一次回执

关键的三步是：

```python
messages.append(message)
result = add(arguments["a"], arguments["b"])
messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": str(result)})
```

第一行保留“模型要求做什么”；第二行本地执行；第三行记录“执行后的结果”。`tool_call_id` 必须使用这一次模型返回的 ID，就像把结果贴到正确的请求上。同一个 `add` 可以被调用多次，仅凭函数名不能区分每一次调用。

`content` 按本课程所用的工具结果格式填写字符串，因此数值结果用 `str(result)` 转成 `"42"`。如果工具返回复杂字典，可以使用 `json.dumps(result, ensure_ascii=False)` 转成 JSON 文本。

`tool_calls` 是列表，因为一条助手消息可能提出多个调用。代码遍历它，并给每个调用追加对应结果。不能只回一个结果，却把其他调用悬空。

本节为了展示一次完整往返，第二次请求设置 `tool_choice="none"`，要求模型给文字答案。下一章取消这个固定安排，让循环根据模型返回的数据继续或结束。

### 常见误区

只有 `{"role": "tool", "content": "42"}` 不够完整，缺少关联 ID；只追加工具结果却漏了助手的工具调用消息也不对。模型不会读取你电脑上名为 `result` 的变量，必须再次请求才能看到新数据。

### 课后习题

- **8A 预测：**执行完 `result = add(...)`，但尚未发出第二次请求时，远端模型知道这个本地执行结果了吗？
- **8B 修改：**把题目改成 8 加 9，观察调用 ID、参数和结果。
- **8C 补写：**给定 `tool_call` 与 `result`，写出要追加的工具消息，解释三个字段。

<a id="chapter9"></a>
## 第 9 章：搭建完整 Agent 循环

### 具体问题：工具执行一次后，还可能需要再执行一次，怎么办？

前一章手写了两次请求。现在让每一轮都做相同的事：请求模型，检查有没有工具调用，有就执行并追加结果，没有就取出最终答案。

```text
用户问题 → messages → 请求模型
                         ↓
                   是否要求调用工具？
                    ↓是          ↓否
              本地执行函数     返回文字答案
                    ↓
              追加工具结果
                    ↓
                再请求模型
```

这里 `range(max_steps)` 给出有限轮数；`message.get("tool_calls")` 读取可选字段；`if not tool_calls` 在字段缺失、为 `None` 或为空列表时成立。正常响应还需有可用的文字，代码才把它当成完成。

下面是完整的 Agent 主程序。HTTP 函数仍使用第 5 章已经逐段说明的 `api_client.py`，其他工具定义、执行和循环全部写在这里。

完整程序：[09_mini_agent.py](示例程序/09_mini_agent.py)

```python
"""第 9 章：模型选择工具，本地执行，结果进入下一轮。最多五次 API 调用。"""

import json
from api_client import ask_llm


def add(a, b):
    return a + b


def multiply(a, b):
    return a * b


tool_implementations = {"add": add, "multiply": multiply}
tool_definitions = [
    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "计算两个数字的和。",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multiply",
            "description": "计算两个数字的乘积。",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
        },
    },
]


def run_agent(question, max_steps=5):
    messages = [
        {
            "role": "system",
            "content": (
                "你是计算助手，所有加法和乘法必须调用提供的工具。"
                "有依赖关系的计算要先取得前一步工具结果，再调用下一步工具。"
                "工具结果足够后，用一句简短的话给出最终答案。"
            ),
        },
        {"role": "user", "content": question},
    ]

    for step in range(max_steps):
        print("\n第", step + 1, "轮，调用前的 messages：", messages)
        message = ask_llm(messages, tools=tool_definitions)
        print("模型返回：", message)
        messages.append(message)
        tool_calls = message.get("tool_calls")

        if not tool_calls:
            answer = message.get("content")
            if not answer:
                raise ValueError("模型既没有给出工具调用，也没有给出回答")
            print("最终回答：", answer)
            return answer

        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            try:
                arguments = json.loads(tool_call["function"]["arguments"])
                if name not in tool_implementations:
                    raise ValueError("未知工具：" + name)
                a = arguments["a"]
                b = arguments["b"]
                if type(a) not in (int, float) or type(b) not in (int, float):
                    raise TypeError("a 和 b 必须是数字")
                selected_tool = tool_implementations[name]
                result = selected_tool(a, b)
                result_text = str(result)
                print("工具执行：", name, arguments, "结果：", result)
            except (KeyError, TypeError, ValueError) as error:
                result_text = "工具执行失败：" + str(error)
                print(result_text)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": result_text,
            })

    print("达到模型调用次数上限，任务尚未完成。")
    return None


if __name__ == "__main__":
    run_agent("先算 23 加 19，再把结果乘以 7。请按顺序使用工具。")
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\09_mini_agent.py'
```

日志会打印完整消息列表。把细节缩起来后，成功路径通常是：

```text
第 1 轮：发送问题
模型提出 add(a=23, b=19)
工具执行结果：42

第 2 轮：发送问题、第一次调用和结果
模型提出 multiply(a=42, b=7)
工具执行结果：294

第 3 轮：发送包含两次调用结果的消息
最终回答：最终结果是 294。
```

这只是预期路径，实际是否按顺序调用要检查日志，不能因为答案恰好是 294 就认定工具流程正确。本次真实验证已经观察到这三轮，记录在 [完整 Agent 的运行日志](验证/09_mini_agent.py.log) 中。

### 逐段解释：把长程序拆回已经认识的部分

**工具部分。** 两个函数真正计算；`tool_implementations` 在本地按名字查函数；`tool_definitions` 告诉远端模型工具用法。后两者的名称必须对应。

**初始消息。** 系统说明要求计算使用工具，有依赖的计算等待前一步结果。用户问题与固定说明分开存储。这里用相邻字符串自动拼接来把较长说明分成几行，运行时仍是一个字符串。

**每一轮。** `step` 从 0 开始，所以打印时加 1。先得到完整消息并追加，再检查是否有调用。如果有，就执行内层 `for`，逐个追加工具结果；内层循环结束后，自然进入外层下一轮，不需要另外写 `continue`。

**工具执行。** `try` 里的代码解析参数、检查工具是否存在、检查两项参数的类型，然后调用函数。`type(a) not in (int, float)` 在这里要求整数或浮点数，也避免把布尔值当作数字。捕获缺键、类型错误和无效 JSON 等错误后，把错误文本作为该调用的结果发回，模型下一轮才有机会修正。

**正常结束。** `return answer` 离开整个 `run_agent` 函数，当然也结束其中的循环。`return` 与 `break` 不同：`break` 只退出当前那层循环。

**达到上限。** 如果五轮都没获得最终回答，程序走到循环后面，打印“任务尚未完成”并返回 `None`。不会伪造最终答案，也不会悄悄发第六次请求。

**入口。** `if __name__ == "__main__":` 表示直接运行这个文件时才启动任务。如果被另一个程序导入，就只定义函数和变量。这便于验证程序本身，不会因导入意外消费额度。

### ReAct 在这段代码里是什么？

模型根据当前消息决定下一步行动，程序执行行动，把观察结果加入下一次消息；模型再决定行动或回答。这条“决定 → 行动 → 观察 → 再决定”的循环体现了 ReAct 的核心思路。

理解它不要求模型输出 `Thought:`，也不要求打印内部推理。这里观察的是实际返回的工具选择、参数和结果。一个 Agent 也不只是一条 `while`：循环中还需要消息组织、模型调用、工具执行和明确的停止判断。

### 常见误区

`max_steps=5` 限制的是模型请求轮数，不是恰好执行五个函数；一条响应可能含多个调用。工具失败后把错误发回，只是提供恢复机会，不保证模型一定会修复。模型给出文字也不自动证明答案正确，计算结果应与日志对应。

### 课后习题

- **9A 预测：**如果一轮返回两个工具调用，这一轮请求了几次模型、最多执行了几个工具？
- **9B 修改：**把题目改成“先算 6 乘 7，再把结果加 8”，检查日志里的先后顺序。
- **9C 补写：**暂时不看完整代码，用伪代码写出请求、完成分支、工具分支、结果追加和上限；再对照补成 Python。

<a id="chapter10"></a>
## 第 10 章：停止与排错

### 具体问题：程序停了，是完成了，还是失败了？

先把错误处理从联网过程里拿出来，用本地数据观察。

完整程序：[10_errors_and_limits.py](示例程序/10_errors_and_limits.py)

```python
"""第 10 章：用手写决定观察失败与上限，不调用 API。"""


def add(a, b):
    return a + b


tools = {"add": add}
decisions = [
    {"name": "divide", "arguments": {"a": 2, "b": 3}},
    {"name": "add", "arguments": {"a": 2}},
    {"name": "add", "arguments": {"a": 2, "b": 3}},
]

for decision in decisions:
    try:
        name = decision["name"]
        if name not in tools:
            raise ValueError("未知工具：" + name)
        arguments = decision["arguments"]
        result = tools[name](arguments["a"], arguments["b"])
        print("工具成功：", result)
    except (KeyError, TypeError, ValueError) as error:
        print("工具失败：", str(error))

max_steps = 3
for step in range(max_steps):
    print("模拟模型连续请求工具，第", step + 1, "轮")
print("达到上限，停止；停止不等于任务完成。")
```

### 运行方法与预期输出

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\教材\示例程序\10_errors_and_limits.py'
```

```text
工具失败： 未知工具：divide
工具失败： 'b'
工具成功： 5
模拟模型连续请求工具，第 1 轮
模拟模型连续请求工具，第 2 轮
模拟模型连续请求工具，第 3 轮
达到上限，停止；停止不等于任务完成。
```

### 逐段解释：先找失败发生在哪一层

`raise ValueError(...)` 主动表示“这个工具不在本地工具字典里”。取不到 `arguments["b"]` 时，Python 抛出 `KeyError`。`except (...) as error` 接住指定种类的异常，再把说明打印出来。这里没有把所有异常一律吞掉，方便看见程序本身的其他问题。

| 现象或异常 | 发生阶段 | 优先检查 |
|---|---|---|
| `SyntaxError` | Python 解析代码 | 中文引号、缺逗号、括号、缩进 |
| `NameError: user` | 本地执行 | 角色字符串是否漏写引号 |
| `FileNotFoundError` | 读取配置 | 密钥文件名和目录层级 |
| `KeyError: 'content'` | 本地取值 | 正在读哪层字典，消息是否只有工具调用 |
| `TypeError` | 参数传递或运算 | 是否少传参数、是否把字符串当数字 |
| `HTTPError` 401 | API 鉴权 | 密钥是否有效、是否读取正确 |
| `HTTPError` 402 | API 账户额度 | 账户是否有可用余额 |
| `HTTPError` 400/422 | API 请求校验 | 模型参数、工具 schema、消息关联是否正确 |
| `HTTPError` 429 | 服务限流 | 降低请求频率，先暂停 |
| `HTTPError` 5xx | 服务端 | 记录发生时间，稍后再检查 |
| `URLError` / 超时 | 网络 | 连接、代理、网络等待是否异常 |
| `JSONDecodeError` | JSON 解析 | 解析的是完整 JSON 还是普通文本 |
| `finish_reason='length'` | 模型生成 | 回答因上限截断；缩短任务或另外调整限额 |

具体状态解释以 [DeepSeek 错误码文档](https://api-docs.deepseek.com/quick_start/error_codes) 为准。遇到 HTTP 错误，先看状态码，不要把密钥或整个请求头复制到提问内容中。

第 9 章只把**工具阶段**的已知错误转成工具结果。网络阶段失败时，程序会抛出异常并停止，不会假装那是工具成功，也不会自动不断重试。对于初学者，看见失败位置比在后台反复发送请求更容易理解。

### 三种结束需要分清

| 情况 | 解释 |
|---|---|
| 收到正常最终文字，`return answer` | 本次循环认为任务完成 |
| 达到 `max_steps`，`return None` | 次数用尽，任务未完成 |
| 抛出网络或生成异常 | 本次执行失败，需要定位原因 |

如果模型一直调用工具，上限能让程序停下来。如果想检查“上限是否有用”，可以用本章离线示例或验证脚本，不必真实消耗五轮 API。

### 常见误区

`except` 不会自动修好参数；它只是让程序有机会处理失败。看到 HTTP 异常通常意味着服务器已经返回了错误，不能一律解释成没有响应。看到工具错误后模型继续运行，也不等于工具之前执行成功。

### 课后习题

- **10A 预测：**未知工具的决定会走到 `tools[name](...)` 吗？为什么？
- **10B 修改：**把第二个决定补上 `b=8`，观察它从失败变成成功。
- **10C 补写：**用自己的话列出最终回答、上限退出、网络错误三条退出路径，并指出第 9 章分别在哪里发生。

<a id="exercises"></a>
## 课后习题汇总

各章末尾的 A/B/C 题分别对应预测、修改、补写。你可以只做一个章的练习就停止，不需要一次把全书做完。

| 学习阶段 | 完成标志 |
|---|---|
| 第 1～2 章 | 能分别追踪列表内容与函数返回值 |
| 第 3～4 章 | 能说出字典、文本、字节的转换和真正联网的位置 |
| 第 5～6 章 | 能追加消息，按名字找到工具并传入参数 |
| 第 7～8 章 | 能解析参数字符串，把结果关联到正确调用 |
| 第 9～10 章 | 能解释完整循环的继续、完成和失败路径 |

**综合练习：**给最终 Agent 新增减法工具，要求它“先算 20 减 3，再乘以 4”。需要修改哪些地方？不要只看最终 68，还要在日志中找到减法调用、结果 17、后续乘法参数以及结果 68。参考答案在文末，但建议先独立标出需要修改的位置。

<a id="mistakes"></a>
## 常见误区速查

| 容易产生的理解 | 更准确的理解 |
|---|---|
| `len(messages)` 会数内部所有键值 | 只数外层列表元素 |
| 一条字典写两次 `role` 能表示两条消息 | 同名键覆盖；两条消息用两个字典 |
| `append` 返回加入的元素 | 修改原列表，返回 `None` |
| 函数最后写什么就自动返回什么 | 没有显式 `return` 就返回 `None` |
| 参数在函数里出现，外面就一定不变 | 对同一个可变对象的修改可以在外面看到 |
| `items = []` 与 `items.append(...)` 一样 | 前者重新绑定局部名字，后者修改对象 |
| `add(arguments)` 不行，因为函数不能接收字典 | 当前函数需要两个参数，该调用只传了一个 |
| `arguments[a]` 就是在取键 `a` | 无引号的 `a` 是变量；字符串键写 `"a"` |
| `history_content` 是历史消息专用字段 | 历史正文仍放 `content`，位置和角色构成对话 |
| API 就是密钥 | API 是接口，API key 是访问凭证 |
| 请求正文只有用户的问题 | 还包含模型名、消息列表、工具和生成选项 |
| 模型返回工具名称就已经执行了函数 | 本地分发代码才实际执行 |
| 发送 Python 函数就能让服务器调用 | 发送工具说明，本地保存和运行函数 |
| 解析过响应，`arguments` 就必然是字典 | 该字段按协议仍是 JSON 字符串，需要再解析 |
| 工具结果只要写 `content` 就够了 | 还要保存原助手调用，并用 ID 关联结果 |
| `finish` 是每个 Agent 必须提供的工具 | 可以直接以正常最终回答结束 |
| ReAct 必须展示模型内部思考 | 可以通过行动和观察的循环理解 |
| 限制五轮就等于限制账户总费用 | 只限制当前任务的模型调用轮数 |
| `len()` 能用于所有 Python 对象 | 可用于本课的列表、字典、字符串等；整数就不支持 |

最后一条也修正了你 Python 摘要中“所有对象都能用 len”的过度概括。学习摘要适合提醒学过什么，遇到行为问题仍以实际 Python 规则为准。

<a id="answers"></a>
## 参考答案

### 第 1 章

**1A：**都是 2，但数的对象不同：前者是两条消息，后者是第一条消息的两个键。

**1B：**

```python
messages.append({"role": "user", "content": "继续"})
print(messages[2]["content"])
```

**1C：**

```python
choices = response["choices"]
first_choice = choices[0]
answer = first_choice["message"]["content"]
# 合并：
answer = response["choices"][0]["message"]["content"]
```

### 第 2 章

**2A：**`items` 为 `[7]`，`result` 为 `None`。

**2B：**在 `items.append("你好")` 后增加 `return "已添加"`。列表变化与返回字符串同时存在。

**2C：**

```python
def add_user_message(messages, text):
    messages.append({"role": "user", "content": text})
    return len(messages)
```

### 第 3 章

**3A：**解析前是 `str`，解析后是 `dict`。

**3B：**修改原始 `data["content"]`，其余转换不变；恢复后的 `received_data["content"]` 应为“我叫小林”。

**3C：**

```python
text = json.dumps(data, ensure_ascii=False)
body = text.encode("utf-8")
received_text = body.decode("utf-8")
restored = json.loads(received_text)
```

### 第 4 章

**4A：**不会。这里只创建请求对象，没有调用 `urlopen`。

**4B：**修改 `messages` 中用户消息的 `content`。响应措辞不固定。

**4C：**准备字典 → `dumps` 和 `encode` 得到字节 → `urlopen` 发请求并 `read` 读响应 → `decode` 和 `loads` 得到字典 → 沿 `choices[0]["message"]["content"]` 取答案。

### 第 5 章

**5A：**4 条：system、user、assistant、user。

**5B / 5C：**在已有对话后增加：

```python
messages.append({"role": "user", "content": "请把我的名字重复两遍"})
assistant_message = ask_llm(messages)
messages.append(assistant_message)
print(assistant_message["content"])
```

### 第 6 章

**6A：**没有，仅保存函数对象。

**6B：**`decision = {"name": "multiply", "arguments": {"a": 6, "b": 7}}`，结果 42。

**6C：**

```python
def subtract(a, b):
    return a - b

tools["subtract"] = subtract
name = "subtract"
arguments = {"a": 10, "b": 3}
result = tools[name](arguments["a"], arguments["b"])
```

### 第 7 章

**7A：**`str`。

**7B：**修改 description 只改变发给模型的说明，不会重写本地 `add`。

**7C：**复制加法工具定义，将名字改成 `multiply`、说明改成“计算两个数字的乘积”；参数结构仍是数值 `a` 和 `b`。完整格式可以对照第 9 章。

### 第 8 章

**8A：**尚未收到这个执行结果；本地改变不会自动传输。

**8B：**修改用户正文，预期参数 8、9，执行结果 17；调用 ID 由服务返回，不手写固定 ID。

**8C：**

```python
messages.append({
    "role": "tool",
    "tool_call_id": tool_call["id"],
    "content": str(result),
})
```

三个字段分别表示消息来自工具、回答的是哪次调用、实际执行结果是什么。追加它之前须保留助手原始调用消息。

### 第 9 章

**9A：**一次模型请求，两个工具调用。执行它们本身不会再次调用模型。

**9B：**预期先调用乘法得到 42，再调用加法得到 50；日志比最终数字更能证明流程。

**9C：**有限循环内：请求并保存助手消息 → 无工具且有正常答案时返回 → 否则逐个解析并执行工具 → 保存带 ID 的结果 → 进入下一轮。用尽次数后明确报告未完成。Python 对照第 9 章主循环即可。

### 第 10 章

**10A：**不会，前面的检查主动 `raise`，流程跳到匹配的 `except`。

**10B：**第二个决定改为 `{"name": "add", "arguments": {"a": 2, "b": 8}}`，得到 10。

**10C：**完成在 `return answer`；上限在循环之后 `return None`；网络错误在调用 `ask_llm` 时向外抛出。本例没有为网络失败自动重试。

### 综合练习

新增 `subtract` 函数，把它加入 `tool_implementations`，在 `tool_definitions` 中加入同名工具说明，然后修改系统说明让减法也使用工具，并修改用户问题。现有主循环按名字分发，三个工具都使用数值 `a`、`b`，因此不需要增加一个新的分支。若新工具参数结构不同，就需要调整分发方式，不能机械照搬 `a`、`b`。

<a id="optional"></a>
## 选学附录：保存历史、finish、流式输出

### A．保存历史：把列表放到磁盘

主线中的历史随进程结束而消失。下面把消息列表转成 JSON 写入文件，再读回来。它只验证保存和恢复，不调用 API。`indent=2` 让文件按两格缩进，便于阅读。

[11_save_history.py](示例程序/11_save_history.py)

```python
"""选学：保存并恢复消息。离线运行，会在本目录写入 demo_history.json。"""

import json
from pathlib import Path

messages = [
    {"role": "user", "content": "我叫小林。"},
    {"role": "assistant", "content": "你好，小林。"},
]
history_file = Path(__file__).resolve().parent / "demo_history.json"
text = json.dumps(messages, ensure_ascii=False, indent=2)
history_file.write_text(text, encoding="utf-8")

saved_text = history_file.read_text(encoding="utf-8")
restored_messages = json.loads(saved_text)
print("恢复后的消息：", restored_messages)
print("恢复后仍然是列表：", type(restored_messages))
print("保存位置：", history_file)
```

运行时把通用命令的文件名改成 `11_save_history.py`。预期恢复出相同的两条消息，类型仍为 `list`；文件 `demo_history.json` 写在脚本所在目录。再次运行会覆盖这份演示文件。

恢复之后，还需要把列表作为 `messages` 传给请求函数，模型才会看见历史。如果保存的是含工具调用的对话，要保留调用和结果的对应关系，不能只截取工具结果孤立发送。

### B．finish：可选的结束协议

主线直接在模型给出正常文字时结束。另一种设计是给模型一个名为 `finish` 的工具，由程序识别它并结束。这个名字并不是 Python 内置功能，也不是设置后服务器就自动停止你本地循环。

[12_finish_demo.py](示例程序/12_finish_demo.py)

```python
"""选学：finish 是你可以自己设计的一种结束协议。离线运行。"""

# 这是本地教学协议，不是 DeepSeek 原始响应的格式。
decision = {"name": "finish", "arguments": {"answer": "最终结果是 294。"}}

name = decision["name"]
arguments = decision["arguments"]
if name == "finish":
    final_answer = arguments["answer"]
    print(final_answer)
    print("程序识别到 finish，结束本次任务。")
else:
    print("这里继续执行普通工具。")
```

运行后输出“最终结果是 294。”以及程序识别完成的说明。这是本地简化协议示范，没有声称模型真实调用过 finish。若接到真实接口，还需发送 finish 的工具定义，解析真实 `tool_calls`，并让自己的循环执行退出分支。

### C．流式输出：完整响应拆成多次事件

普通调用等待完整响应后读取；流式调用在连接中逐步读取事件。事件里通常是新增片段，可能一个字、几个字或为空，不保证“一个事件就是一个字”，也不一定重复发送已经出现的整段答案。

本例只演示纯文本。SSE 是服务向客户端持续发送文本事件的一种格式；该接口的数据行以 `data:` 开头，末尾有 `[DONE]` 标记。`line[5:]` 去掉前面的 `data:`，`delta` 中的 `content` 是本次新增文本。

[13_stream_text.py](示例程序/13_stream_text.py)

```python
"""选学：纯文本流式输出。调用一次 API；不处理流式工具调用。"""

import json
import urllib.request
from pathlib import Path

workspace = Path(__file__).resolve().parents[2]
api_key = (workspace / "DeepSeekKey（UTF8）.txt").read_text(encoding="utf-8-sig").strip()
data = {
    "model": "deepseek-flash",
    "messages": [{"role": "user", "content": "用一句话解释什么是 Python 列表。"}],
    "stream": True,
    "thinking": {"type": "disabled"},
    "max_tokens": 512,
}
request = urllib.request.Request(
    "https://api.deepseek.com/chat/completions",
    data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
    method="POST",
)

finish_reason = None
with urllib.request.urlopen(request, timeout=30) as response:
    # 此接口使用 SSE，每个 data 行装着一段 JSON 或 [DONE]。
    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        event_text = line[5:].strip()
        if event_text == "[DONE]":
            break
        event = json.loads(event_text)
        choices = event.get("choices", [])
        if not choices:
            continue
        choice = choices[0]
        piece = choice["delta"].get("content")
        if piece:
            print(piece, end="", flush=True)
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]
print()
if finish_reason != "stop":
    raise RuntimeError("流式回答未正常完成：" + str(finish_reason))
```

运行会调用一次付费 API，预期逐步显示一句关于列表的解释。`print(..., end="", flush=True)` 表示不自动换行，并尽快把输出显示出来。代码跳过没有正文的事件，并检查结束原因，避免把截断的文本当完整回答。

这个示例针对 DeepSeek 的 Chat Completions SSE 格式，不是所有 SSE 服务的通用解析器。流式工具调用还涉及把多段参数拼起来，再解析 JSON；主线先掌握非流式工具调用即可。

<a id="sources"></a>
## 官方资料与验证说明

本教材使用真实接口字段，以下是核对入口：

- [DeepSeek 首次调用 API](https://api-docs.deepseek.com/zh-cn/)：地址、模型名和基本调用。
- [Chat Completions 参数与响应](https://api-docs.deepseek.com/api/create-chat-completion/)：`messages`、`tools`、`tool_choice`、`finish_reason` 等。
- [工具调用](https://api-docs.deepseek.com/guides/tool_calls/)：助手调用与工具结果的配对。
- [思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)：本书显式关闭该模式。
- [模型价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：费用计算以服务实际计费为准。
- [错误码](https://api-docs.deepseek.com/quick_start/error_codes)：HTTP 错误的说明。
- [Python 数据结构](https://docs.python.org/zh-cn/3.13/tutorial/datastructures.html)、[函数](https://docs.python.org/zh-cn/3.13/tutorial/controlflow.html#defining-functions)、[JSON](https://docs.python.org/zh-cn/3.13/library/json.html)、[urllib.request](https://docs.python.org/zh-cn/3.13/library/urllib.request.html)：基础行为的官方参考。

接口可能更新。文中的运行环境和验证日期已标明；真实接口验证、离线模拟验证及未做的验证，统一记录在 [验证记录.md](验证记录.md)。
