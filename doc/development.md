# 开发指南

面向想在源码上改动的开发者。代码分三块:C++17 代理(`proxy/`)、Python/Flask 仪表盘(`app/` + `server.py`)、前端 SPA(`static/` + `templates/`)。总体结构见 [architecture.md](architecture.md)。

## 构建代理

依赖分两类。系统库:OpenSSL、Threads(CMake `find_package`)。vendored 在 `proxy/third_party/`:`httplib.h`(HTTP 客户端/服务端)、`json.hpp`、`sqlite3.c/h`,无需额外下载。`sqlite3.c` 以 C 编译,开启 `SQLITE_THREADSAFE=1`、FTS5、JSON1。

```bash
cd proxy
cmake -B build -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE=/home/redrocker/miniconda3/bin/python
cmake --build build -j$(nproc)
cd ..
PYTHONPATH=. python3 -m app.db.schema_upgrade.cli \
  --token-board-db data/token-board.db \
  --dashboard-db data/dashboard.db \
  --schema-dir schema \
  --timezone Asia/Shanghai
cd proxy
./build/token_proxy --db ../data/token-board.db --port 8800
```

`-DPython3_EXECUTABLE` 指定测试用的解释器（`BUILD_TESTING` 默认开启）。解析规则、`PY` 兜底与 configure 期依赖校验见「Python 解释器与测试环境」；只构建生产二进制时可用 `-DBUILD_TESTING=OFF` 跳过测试解释器的解析与校验。

数据库升级只在 `bash start.sh --all` 中执行。代理的手动启动和
`token-maintenance` 都只做 current schema 验证；`--schema-dir` 仅为旧启动命令
保留，C++ 不读取 SQL、不创建数据库、不执行升级，直接启动旧数据库会快速失败。

Release 构建额外加 `-O3 -march=native -flto`。构建产物是 `token_proxy`(完整代理)和 18 个测试可执行文件；`BUILD_TESTING=OFF` 时只构建 `token_proxy`。`format_conv_test` 是其中一个 codec 自测(见下)。

## codec 自测

`format_conv_test` 只依赖 codec 层,不碰 sqlite / http,适合快速验证格式转换。

```bash
./build/format_conv_test --self-test
```

内嵌往返矩阵覆盖:OpenAI→Anthropic 流式工具调用、reasoning→thinking 块、Anthropic tool_result→OpenAI tool 消息、`[1m]` 后缀剥离、tool_choice 归一化等。也可以单发转换:

```bash
./build/format_conv_test --request --from openai --to anthropic < body.json
./build/format_conv_test --response --from anthropic --to openai < resp.json
./build/format_conv_test --stream --from openai --to responses < body.json
./build/format_conv_test --request --from anthropic --to google < body.json
```

## 模拟上游

`scripts/mock_upstream.py` 起一个模拟 opencode.ai "Console Go" 的服务,支持全部四种格式,用于无真实上游时调试代理。请求会写进 `/tmp/mock_upstream.log`。

```bash
python3 scripts/mock_upstream.py --port 9100
```

它复刻了真实上游的严格校验:chat completions 拒绝 `role: developer` 消息、拒绝非 `function` 类型的工具、拒绝 `parameters` 不是 `{"type":"object"}` 的工具。请求体里可以塞测试钩子:`mock_format`(指定响应格式)、`mock_status`(指定返回状态码)、`mock_delay`、`mock_tool`、`mock_simple_stream`(无 reasoning 的纯流)。

## 测试隔离

测试代码与业务代码按层次使用固定的主隔离策略；新测试沿用所在层次的策略，只有技术上确不可行时才引入例外，并把适用范围、理由与边界登记在本节。

- **目录与命名**：C++ 测试可执行文件与 Python 端到端脚本都在 `proxy/tests/`(`*_test.cpp` / `*_test.py`)；应用层测试在 `app/tests/`(pytest `testpaths` 只收集这里，`test_*.py`、`*.js`)。测试代码不放进 `proxy/src/` 或 `app/` 的业务目录。
- **构建**：测试目标只定义在 `proxy/tests/CMakeLists.txt`、只随 `BUILD_TESTING` 存在且不链接进 `token_proxy`。测试目标自行编译它要覆盖的生产源文件；测试专用编译宏只定义在测试目标上，生产目标与生产库从不定义这类宏。
- **运行时外部依赖**：C++ 测试在进程内起 `httplib::Server` 或裸 socket mock；Python 端到端测试用 `scripts/mock_upstream.py` 配 `proxy/tests/v1_fixture.py` 搭建上游与数据库；不引入第三方 mock 框架。
- **配置与环境变量**：生产参数(`TB_MAX_WORKERS`、`TB_TASK_QUEUE_MAX`、`TB_COOLDOWN_PROBE_SECS`、`TB_USAGE_WATCH_SECS`、`TB_LEDGER_REFRESH_SECS`、`PROXY_STRICT_TERMINAL`)的默认值即生产行为，测试用它们压缩时间或灰度；只服务测试脚本的标志(`TB_TEST_DEBUG`)由脚本自己读取，不进入任何二进制。
- **端到端**：Python 集成测试启动真实 `token_proxy` 二进制(CTest 传 `$<TARGET_FILE:token_proxy>`)，断言对外行为与落库结果。

当前唯一的例外是崩溃注入点：`accounting_recovery` 需要在「spool 已 fdatasync、SQLite 尚未提交」的精确时刻终止进程，mock 与配置层表达不了，因此在构建层例外——`durable_spool.cpp` 中 `#ifdef TB_ENABLE_TEST_CRASH` 的注入点只随测试目标 `token_proxy_test` 存在(该目标在 `tests/` 下自行编译 store 源文件并定义宏)，生产库永不定义该宏。边界由 `crash_hook_isolation` 用例强制:生产二进制必须不含 `TB_TEST_CRASH_AFTER_SPOOL_SYNC` 标记，测试二进制必须含。

## Python 解释器与测试环境

`app_testbench`（`python -m unittest discover` 收集 `app/tests`）与 `proxy/tests` 的端到端脚本共用 configure 解析出的同一个 Python 解释器，并导入 `flask`、`requests`、`jinja2`（`jinja2` 由 Flask 带入）。解释器在 configure 阶段解析一次、写进构建树缓存；如果解析到缺依赖的解释器，失败会推迟到测试运行期、表现为 `ModuleNotFoundError`，所以 configure 先做一次导入自检，不通过就中止。

### 解析到缺依赖解释器的原因

未显式指定时，`find_package(Python3 COMPONENTS Interpreter REQUIRED)` 按 CMake 的搜索顺序挑一个 `python3`：PATH 上的系统解释器、`uv` 安装的解释器、激活的 venv（其 `bin` 在 PATH 最前）都是候选，而 conda base 不在 PATH 上。本机 PATH 上的 `python3` 是 `/usr/bin/python3`（3.13.5，缺 `flask`/`jinja2`），`~/.local/bin/python3.13` 是 uv 装的 3.13.15（`flask`/`requests`/`jinja2` 都缺）；没有激活 venv 时直接 configure 会选中后者，导入自检随即失败。带依赖的是 `/home/redrocker/miniconda3/bin/python`（conda base，3.13.11）。

解析结果缓存在构建树的 `Python3_EXECUTABLE` 里：已有缓存的构建树继续用缓存值，新建构建目录（或清掉缓存）才会重新解析。因此同一个仓库里可能同时存在能跑和不能跑的构建树，取决于它是用哪个 PATH、哪个解释器 configure 出来的。

### 显式指定解释器

```bash
cmake -S proxy -B proxy/build \
  -DPython3_EXECUTABLE=/home/redrocker/miniconda3/bin/python
```

`/home/redrocker/miniconda3/bin/python` 可以换成任意带 `flask`、`requests`、`jinja2` 的解释器。需要复用时先存进变量：

```bash
PY=/home/redrocker/miniconda3/bin/python
cmake -S proxy -B proxy/build -DPython3_EXECUTABLE="$PY"
```

`-DPython3_EXECUTABLE` 是 cache 条目，显式传入会覆盖构建树里已有的缓存值。

### PY 环境变量兜底

`proxy/tests/CMakeLists.txt` 的 `tb_prepare_python3_for_tests()` 在 `find_package` 之前读取环境变量 `PY`，把它作为 `Python3_EXECUTABLE` 的默认 cache 值：

```bash
export PY=/home/redrocker/miniconda3/bin/python
cmake -S proxy -B proxy/build
```

优先级：显式 `-DPython3_EXECUTABLE` > 构建树已有缓存 > `PY` > `find_package` 自行搜索。`PY` 不覆盖已有缓存；三者都没有时由 `find_package` 自行搜索，搜索到的解释器仍要过导入自检。

### configure 期依赖校验

`proxy/tests/CMakeLists.txt` 与 `app/tests/CMakeLists.txt` 都调用 `tb_require_python_test_deps()`：用解析出的解释器执行 `import flask, requests, jinja2`，退出码非 0 就 `FATAL_ERROR`。报错给出实际解释器路径、缺失模块、必需模块、解释器的原始错误，以及一条带当前构建目录的修复命令：

```text
CMake Error at tests/CMakeLists.txt:43 (message):
  The Python interpreter used by the test suite is missing dependencies.

    interpreter:      /home/redrocker/.local/bin/python3.13
    missing module:   flask
    required modules: flask, requests, jinja2
    interpreter says: Traceback (most recent call last):
      ...
    ModuleNotFoundError: No module named 'flask'

    fix: re-run cmake with an interpreter that provides them, for example:
      cmake -S proxy -B /home/redrocker/File/Token-Board/proxy/build -DPython3_EXECUTABLE=/home/redrocker/miniconda3/bin/python
```

含义是这次 configure 选中的解释器没有测试依赖，不是测试失败：按 `fix` 行对同一个构建目录重跑 configure 即可。无论解释器来自 `-D`、`PY` 还是 `find_package` 搜索，都走这项校验；`-DBUILD_TESTING=OFF` 的构建不解析测试解释器，也不做这项校验。

### python_env 用例

`python_env` 把「解释器带依赖」固化成一条 CTest 结果：命令是 `${Python3_EXECUTABLE} -c "import flask, requests, jinja2"`，标签 `unit`，超时 300 秒。它和 configure 期的导入检查是同一条命令，所以 configure 通过后它必然通过；作用是在直接跑 `ctest` 的 shell 或 CI 里把「环境不对」和「测试失败」分开：

```bash
ctest --test-dir proxy/build -R '^python_env$' --output-on-failure
```

### 改 tests/CMakeLists.txt 之后

`Python3_EXECUTABLE` 缓存、导入自检和 `add_test` 清单都是 configure 产物。改动 `proxy/tests/CMakeLists.txt` 或 `app/tests/CMakeLists.txt` 后要重新 configure：同一构建目录重跑会保留已缓存的解释器，不需要重复 `-D`；新建构建目录则要重新指定解释器（`-DPython3_EXECUTABLE` 或 `PY`）。CTest 清单的新鲜度由 `ctest_registration_fresh` 校验，见下节。

## CTest 注册

`ctest` 的测试清单和测试属性都是 configure 产物：CMake 只在该步骤把 `add_test()` 与 `TIMEOUT` 写进构建树的 `CTestTestfile.cmake`。因此改动任意 `tests/CMakeLists.txt`（增删 `add_test`、改 `foreach` 的迭代列表）后必须重新 configure，否则构建树继续注册旧清单、静默少跑或多跑用例：

```bash
cmake -S proxy -B proxy/build -DPython3_EXECUTABLE=/home/redrocker/miniconda3/bin/python
```

门禁用例 `ctest_registration_fresh` 由 `scripts/check_ctest_registration.py` 实现，对**同一构建树**做两项检查：

1. **清单一致**：动态计算源侧测试数（`proxy/tests/CMakeLists.txt` 的 `add_test` 声明数 + `app/tests/CMakeLists.txt` 的声明数按时区 `foreach` 迭代列表展开），与 `ctest --show-only=json-v1` 的注册结果比对；数量或名单不一致即失败，打印期望数、实测数、差额与缺失/多余用例名。
2. **超时存在**：每个注册用例都必须有正的 `TIMEOUT`，缺失即失败并列出用例名。`tests/CMakeLists.txt` 按目录统一设置：普通用例 300 秒（含 `app/tests` 的 12 个用例），带 `performance` 或 `soak` 标签的用例 1800 秒。

脚本的 `--build-dir` 由 CTest 传入 `CMAKE_BINARY_DIR`，所以任一构建目录（如 `proxy/build/<task>`）都校验自己的注册清单。手工等价命令：

```bash
/home/redrocker/miniconda3/bin/python scripts/check_ctest_registration.py --build-dir proxy/build --source-root .
ctest --test-dir proxy/build -R '^ctest_registration_fresh$' --output-on-failure
```

只看注册数量和用例名（不做校验，也不检查 `TIMEOUT`）：

```bash
ctest --test-dir proxy/build -N
```

## 测试环境与门禁契约

`ctest` 的 Python 用例、`app/tests` 与 `proxy/tests` 的脚本共用 configure 解析出的同一个解释器。这一节记录四类无法由 configure 或测试套件自身表达的契约（解释器、泄漏、只读树、socket 路径），以及测试注册口径；对应脚本全部以仓库根为工作对象，可手工执行，也可由 CI 逐条调用。

### 解释器缺依赖时的唯一恢复路径

解释器缺依赖时 configure 直接 `FATAL_ERROR`（报错格式见上节「configure 期依赖校验」）。这里没有自动兜底：不会在检测失败后自动改用 conda base 或其它解释器——兜底会把"环境不对"变成静默行为，而测试依赖是否齐全只有调用方知道。**唯一恢复路径**是给 configure 一个带依赖的解释器：

```bash
# 新建构建目录时显式指定
cmake -S proxy -B proxy/build -DBUILD_TESTING=ON \
      -DPython3_EXECUTABLE=/home/redrocker/miniconda3/bin/python

# 或先存入变量再复用（PY 不覆盖构建树已有的缓存）
PY=/home/redrocker/miniconda3/bin/python
cmake -S proxy -B proxy/build -DBUILD_TESTING=ON -DPython3_EXECUTABLE="$PY"
```

`-DBUILD_TESTING=OFF` 的构建不解析测试解释器，也不做这项校验。

### conda base 的入口（conda 不在 PATH、CONDA_PREFIX 未设置）

本机 conda 装在 `/home/redrocker/miniconda3`，但默认 shell 里 `conda` 不在 `PATH`、`CONDA_PREFIX` 未设置，`python3` 落到 `/usr/bin/python3`（缺 `flask`/`jinja2`）。带测试依赖的是 conda base 的解释器，它只能通过绝对路径访问：

```bash
# 直接解释器（configure 的 -DPython3_EXECUTABLE / PY 用它）
/home/redrocker/miniconda3/bin/python -m pytest app/tests -q

# 或让 conda 自己进 base 环境（不需要 activate、不需要修改 PATH）
/home/redrocker/miniconda3/bin/conda run -n base python -m pytest app/tests -q
```

两条命令在 `PATH=/usr/bin:/bin`、`CONDA_PREFIX` 已清除的环境里同样成立，`scripts/check_python_env.py` 的 conda-base 断言验证的正是这一点（该脚本同时断言缺依赖的解释器在 configure 期被拒）。conda 前缀由 configure 选中的解释器推导（`<prefix>/bin/conda` + `<prefix>/conda-meta` 同时存在），也可以 `--conda-prefix` 显式给出，脚本里没有写死路径。

### 泄漏门禁：ResourceWarning 与释放期审计

`pytest.ini` 用两条 `filterwarnings` 把泄漏变成失败：

```ini
filterwarnings =
    error::ResourceWarning
    error::pytest.PytestUnraisableExceptionWarning
```

两条缺一不可，原因是执行顺序：CPython 默认忽略 `ResourceWarning`，第一条把它变成异常；该异常发生在连接终结器里，pytest 的 unraisable 插件把它重新 `warnings.warn` 成 `PytestUnraisableExceptionWarning`，而这个类型不匹配任何 `ResourceWarning` 过滤器，所以需要第二条。

这套过滤器覆盖的是"测试执行期（含下一个用例 setup 时收集的 unraisable）"发生的泄漏。**不覆盖**解释器退出时仍然存活的连接：这类连接不产生 unraisable，也不会进入 pytest 的告警统计。因此泄漏审计另有一条与告警无关的确定性门禁：

```bash
/home/redrocker/miniconda3/bin/python scripts/check_sqlite_leaks.py .
```

它在一次 pytest 会话里把 `sqlite3.connect` 接到一个受审计的子类上：每次连接记录创建位置，`close()` 计数，终结器发现"释放但未关闭"立刻记账，会话结束时再 `gc.collect()` 并扫描仍然存活的连接。输出一行汇总，泄漏时逐条打印创建位置：

```text
[TB-SQLITE-LEAK-AUDIT] opened=N closed=N released_without_close=0 alive_unclosed=0
```

同一脚本默认先跑一次负控（子进程里的故意泄漏套件），确认"释放但未关闭"和"会话结束仍存活"两类都能被抓到，再审计 `app/tests`。**取舍**：`-W error`（全量告警提升为错误）也能让 `app/tests` 全绿，但它是全局开关——任何与泄漏无关的告警（第三方库在特定版本上的 `DeprecationWarning` 等）都会让套件变红。因此只启用上面两条定向过滤器，把失败条件限定在泄漏本身；代价是退出期存活的连接不在这套过滤器视野内，由 `check_sqlite_leaks.py` 的显式审计覆盖。

### 只读源码树与只读构建目录

**pytest 在只读源码树里可直接运行。** `-p no:cacheprovider` 只阻止 `.pytest_cache`；pytest 仍会把断言重写后的 `.pyc` 写进 `__pycache__`。源码树只读时，CPython 对不可写 `.pyc` 静默跳过，pytest 正常通过且不落盘。需要可写工作树也零落盘时，额外设 `PYTHONDONTWRITEBYTECODE=1`（断言重写在内存中进行，`pyc` 只是缓存；要连断言重写一起关掉才用 `-p no:assertion`，代价是失去重写后的报错细节）。

**ctest 在只读构建目录里会直接中止**：CTest 需要写自己的 `<build>/Testing/Temporary/LastTest.log`，写不了就 exit 8、零个测试执行，报错是 `Cannot create log file: LastTest.log`。`--output-log` / `-O` 只改变测试输出的去向，不改 CTest 自身的日志，因此救不了这种情形。最小恢复是把该目录放开写：

```bash
chmod -R u+w proxy/build/Testing/Temporary
```

其余部分可以保持只读。另一种做法是把构建目录整体复制到可写位置再 `ctest`（源码树只读没有影响）。CI 侧应把 exit 8 且没有任何 `Test #` 行的情况识别为基础设施失败，而不是测试失败。

两条门禁分别固定上述行为：

```bash
/home/redrocker/miniconda3/bin/python scripts/check_readonly_pytest_tree.py .
/home/redrocker/miniconda3/bin/python scripts/check_ctest_readonly_recovery.py
```

前者要求"可写副本必须出现 `__pycache__` 且 `-p no:cacheprovider` 生效（0 个 `.pytest_cache`）"，再要求"只读副本套件通过且零落盘"；后者在一个临时最小工程上要求"可写构建树 ctest 通过 → 整树只读 ctest exit 8 且不执行测试 → 只放开 `Testing/Temporary` 后恢复通过"，并断言 `--output-log` 在只读树上仍然 exit 8。

### proxy/tests 的 pytest 可见性

`proxy/tests` 的 `*_test.py` 是 CTest 驱动的端到端脚本：位置参数由 `add_test` 注入，每个脚本都带 `if __name__ == "__main__"`，导入无副作用（模块级不读 `sys.argv`、不执行测试体）。它们不以 pytest 用例的形式存在，因此：

- 仓库根目录裸跑 `pytest`（`testpaths = app/tests`）不会收集 `proxy/tests`，只跑 `app/tests` 的用例；
- 显式 `pytest proxy/tests --collect-only` 会导入这些文件（路径参数不受 `norecursedirs` 影响），当前输出 `no tests collected`（exit 5）、0 个收集错误；若某个脚本导入即失败，会以 exit 2 暴露，不会被静默吞掉；
- `pytest.ini` 的 `norecursedirs` 在 pytest 默认 9 项（`*.egg .* _darcs build CVS dist node_modules venv {arch}`）之后追加 `proxy/tests`，作用是让根目录递归不到这些脚本，而不是隐藏导入错误。

要以 pytest 手工跑 C++ proxy 的测试，只能逐脚本直接执行（参数约定见各脚本文件头），或走 `ctest`。

### app 测试的注册粒度与选择边界

`app/tests` 以一条 CTest 用例承载整套 unittest discovery：

| 事实 | 值 | 复核命令 |
|------|----|----------|
| CTest 条目 | 1 条 `app_testbench` | `ctest -N -V \| grep app_testbench` |
| 承载用例 | `app/tests` 的整套 unittest discovery（`test_*.py` 全部模块） | `python -m unittest discover -s app/tests -t . -p "test_*.py"` |
| 标签 | `app;integration` | `ctest --show-only=json-v1` |
| 超时 | 300 秒 | 同上 |

`proxy/tests` 则是逐脚本、逐目标注册（`add_test` 声明在 `proxy/tests/CMakeLists.txt`），两侧粒度不同属于结构性差异：`proxy/tests` 的每个脚本是一个独立端到端场景，`app/tests` 是一套共享进程级夹具的 unittest 套件，拆成逐模块注册会把解释器启动与数据库夹具成本乘以模块数，并失去单一汇总结果。

选择边界：需要定位单个模块时，用 discover 模式参数直接跑，不需要改注册：

```bash
/home/redrocker/miniconda3/bin/python -m unittest discover -s app/tests -t . \
    -p "test_reset_driven_billing.py"
```

按标签选择整侧用 `ctest -L app`；按名字选择用 `ctest -R '^app_testbench$'`。限制是明确的：`app_testbench` 作为单条用例不能按模块并行，模块内部失败需要读该条用例的完整输出（`--output-on-failure`）。

### AF_UNIX socket 路径上限

maintenance 的唤醒 socket 走 AF_UNIX，内核 `sun_path` 为 108 字节且末字节是 NUL，因此可用路径上限是 **107 字节**。超过时 `socket.bind()` 只报 `AF_UNIX path too long`，不指出上限来自路径长度；`MaintenanceService._prepare_socket()` 因此在 `bind()` 之前先校验，超出即抛 `RuntimeError`，报错含实际字节数、107 上限与完整路径。该异常经 `main()` 的启动失败路径输出 `maintenance startup failed: ...` 并以 exit 2 结束，不再表现为裸 `OSError`。

数据目录（或 `TMPDIR`）过长时，把两者之一移到短路径即可；测试环境用短 `TMPDIR`（例如 `TMPDIR=/tmp/tb-fix`）。门禁：

```bash
/home/redrocker/miniconda3/bin/python scripts/check_maintenance_socket_path.py .
```

它用短路径断言能正常 bind 且权限为 `0o600`，用 173 字节路径断言报错包含长度、上限与路径。

## 脚本

| 脚本 | 用途 |
|------|------|
| `bash start.sh` | 快速前台启动固定端口 5000 的仪表盘；不迁移数据库、不重启后台服务；启动后异步拉取云端配置；`--no-browser` 不开浏览器 |
| `bash start.sh --all` | 编译代理、升级两个数据库、安装/重启 `token-proxy` 与 `token-maintenance`，然后启动前台仪表盘 |
| `bash start.sh --debug` | 根目录入口:暂停 systemd 并在终端输出上下游完整 debug；Ctrl+C 后恢复原服务状态 |
| `bash scripts/start-proxy.sh` | 代理:无参前台启动;`--debug` 暂停 systemd 并在终端输出完整 debug;`--daemon` 后台;`--install`/`--uninstall` 管理 systemd 用户服务 |
| `bash scripts/start-dashboard.sh [--no-browser]` | 兼容入口，转发到统一的 `start.sh`（不再另起第二个仪表盘进程） |
| `bash scripts/status.sh` | 状态检查:代理二进制、proxy/maintenance systemd、8800 健康、仪表盘端口、数据库行数 |

## 数据写入

用量数据来自代理转发和已注册智能体软件:消费报告页点「导出数据」触发 `sync_dashboard`(见 [sync.md](sync.md)),把 `request_log`
按 日×账户/软件(id)×模型 增量聚合写进 `dashboard.db`(纯存档,写时固化的费用直接入库,改价不回溯)。
Agent 用量导入参考 `ref/vibe-usage` 的各来源 parser：每个 adapter 先把 native 数据归一为 `UsageEvent`，再由 `token-maintenance` 的通用 importer 负责游标、幂等和写入 `request_log`；仪表盘打开时通过本地 socket 异步唤醒导入。会话日志导入时按日志时间戳估算请求耗时（claude-code、codex；grok 用原生 `apiDurationMs`），不估算 TTFT：估算耗时写入 `request_log.duration_ms`（不可估时为 0），输出 ≥200 token 且耗时 ≥1000ms 的行另写估算 `output_tps`（含首字等待）。请求日志页对导入行以 `≈N.NN tokens/s` 展示估算速度（无估算显示 `—`），TTFT 显示 `—`；性能监控接口只统计 `source_kind='proxy'` 的代理流量。`project`、`session_id` 只在导入过程的内存中使用,不作为数据库或 API 字段。

### Agent 用量 ref 基准

当前移植基准是 `ref/vibe-usage` 的 commit `415443a3e9e2af0787ab3c2a460166603258a56d`（短 hash：`415443a`，提交信息：`chore: bump version to 0.14.3`，记录日期：2026-10-12），该版本注册 35 个 parser；当前 `ref/vibe-usage` 工作树在 `3f4695e`（`0.15.10`），注册 36 个 parser。本地注册 36 个 adapter，与工作树的 36 个 parser 一一对应：基准之外新增的 `kiki` 与 `kilo` 都已移植，本地没有上游不存在的来源。下次升级 ref 后，可用下面的命令查看新增提交和代码差异：

```bash
git -C ref/vibe-usage log 415443a3e9e2af0787ab3c2a460166603258a56d..HEAD --oneline
git -C ref/vibe-usage diff 415443a3e9e2af0787ab3c2a460166603258a56d..HEAD -- src/parsers
```

本项目已移植全部 36 种智能体来源适配器（`app/services/agent_usage/adapters/`，与 `ref/vibe-usage` 工作树的 36 个 parser 一一对应）：
- **CLI / 转录文件类**：`amp`, `claude-code`, `codebuddy`, `cola`, `copilot-cli`, `craft-agent`, `dimagent`, `droid`, `dsh`, `gemini-cli`, `grok`, `kiki`, `kimi-code`, `omp`, `openclaw`, `pi-coding-agent`, `qwen-code`, `roo-code`, `trae-cli`, `workbuddy`；
- **本地 SQLite / WAL 数据库类**：`alma`, `codearts-agent`, `devin`, `hermes`, `kilo`, `kiro`, `mcode`, `mimocode`, `opencode`, `qoder`, `qoder-cn`, `zcode`（通过 `sqlite_rows_snapshot` 进行安全快照隔离读取，防御并发锁）；
- **混合 / 平台类**：`antigravity`（离线 SQLite + protobuf 转录），`cline`（SDK 会话 + 扩展转录），`codex`（Rollout JSONL + Continuation 段重放分段去重），`cursor`（本地凭据 + CSV 导出校验）；
- **核心设计约定**：
  1. **路由档位命名空间隔离**：`qoder-*`、`codebuddy-*`、`droid-*` 等将本地路由档位（如 `auto`, `default`, `fast` 等）命名空间化，防止与上游公开定价模型产生价格误碰撞；
  2. **防静默变空哨兵**：外部接口或格式变更可能导致误读为 0 Token。`cursor`（表头缺少 Date/Model/Token 列时）与 `grok`（报告有轮次但未读出用量时）实施格式哨兵拦截，返回 `skipped=True` 与报警日志，防止增量同步清空正常用量；
  3. **测试环境约定**：测试与回归验证必须在 `conda base` 环境中执行（`conda run -n base pytest app/tests`）；
  4. **Token 桶口径**：落库的 `prompt_tokens` 含缓存桶（未命中输入 + cache-write + cache-read），`total_tokens = prompt_tokens + completion_tokens`。适配器把源数据的 cache-write 折进 `input_tokens`，cache-read 交给 IR 叠加；只有源数据的 input 本身已含 cache-read（如 `codex`）才传 `input_includes_cache=True`。否则 `prompt_tokens` 会小于 `cache_read_tokens`，dashboard 存档 `daily_model_usage` 的 `cache_read_tokens <= input_tokens` 约束会拒绝该聚合行，并让整批导出回滚。

## 前端

SPA 是 `templates/index.html` + `static/js/` 下的模块,hash 路由,`app.js` 维护页面注册表。ECharts 5.5.0 走 CDN。

- `utils.js`:UTC 时间戳到浏览器当地时间的显示、当地日期筛选、UTC/当地分钟换算、`fmtNum`、`esc`,最先加载。
- `api.js`:fetch 封装 + 各 API 包装。
- `charts.js`:ECharts 渲染层(SVG 2D + 可选 WebGL 3D 日历)。
- `dashboard.js`:用量仪表盘(按月衰减的模型分布与 3D 日历)。模型分布按当前自然月向前每月乘以 `0.9`，以全部模型的加权 Token 总量为分母，严格大于 `1%` 的模型按加权顺序展示；日历使用所有模型的真实 Token 用量，未进入饼图的模型使用中性灰并在图例中合并为 `Other`，悬浮卡片仍展开原始模型名。
- `proxy_manager.js`:账户/聚合/密钥/定价管理页,含模型定价拖放排序、按浏览器当地时间输入的峰谷时段编辑器;档位以 UTC+0 分钟存储。
- `proxy_billing.js`:消费报告 + 请求日志页。
- `agent_manager.js`:智能体订阅与软件来源管理页。

Agent 订阅的实时配置（订阅、实例、价格事件、绑定）与历史身份分离：账单只能通过
`agent_subscription_identities` / `agent_subscription_instance_identities` 的 `id`/`uuid`
归属，不能依赖订阅名或实例标签。删除已有账单的订阅会清理实时图但保留身份和冻结账单，
同名订阅可重新创建；新增字段或引用时要保持这个解耦约束。

全局名称约束：账户、路由、软件、订阅和实例标签的名称都是展示属性，不得新增
`UNIQUE(name)`、名称主键/外键或按名称保存历史关联；业务代码必须使用 `id`/`uuid`。
- `proxy_perf.js`:性能监控页(15s 自动刷新)。
- `proxy_settings.js`:代理设置页(超时三档配置、WebDAV 同步设置)。

## 约定与注意事项

- **schema 只通过升级边界改。** 纯结构变化追加
  `schema/<库>/v<major>/<major>-<minor>_*.sql`；需要数据转换或跨库协调时新增
  `schema/transitions/<transition-id>/transition.json` 和 `transition.py`，并在
  descriptor 中声明 scope、current/prepare/target 版本 route。entrypoint 实现
  `apply(context)` 与 `verify(context)`。两者都由 Python `app.db.schema_upgrade`
  在 shadow/事务中执行，业务 facade、
  reconcile 和 C++ 请求路径不补迁移逻辑。规则见
  [database-migrations.md](database-migrations.md)。
- **已发布的升级代码不可修改。** `schema/<库>/v<major>/*.sql` 和
  `schema/transitions/**` 一旦提交（发布）就冻结：任何字节变化——包括注释、空白和措辞——
  都不能有。`schema_migrations` 为每个已应用文件保存 SHA-256，升级入口每次重算比对；
  被改过的文件会让已升级的库在 `start.sh --all` 或同步升级时报
  `checksum mismatch for recorded migration V<major>.<minor>`，升级卡住。修正只能追加新的
  小版本迁移，不能靠"改旧文件 + 改本机库"消除旧语义：不同时间升级的库会停在不同的实际结构上，
  版本号不再能标识 schema。新迁移在首次提交前可以修改，提交后同样冻结。
- **C++ schema 版本必须同步。** 每次 Token Board Minor tip 增加时，同时更新
  `proxy/src/store/database_lifecycle.cpp` 的
  `kRequiredRuntimeSchemaMinor`，重新编译代理并运行
  `PYTHONPATH=. python3 proxy/tests/schema_version_test.py schema .`。
  Python 已升级数据库而 C++ 仍要求旧 Minor 时，代理会拒绝启动，表现为
  `start.sh --all` 的 `proxy health check failed`。
- **时间存 UTC,显示浏览器当地时间。** 库内 `datetime('now')` 和请求时间存 UTC;前端通过 `Date` 的本机时区显示时间戳,并把当地日期筛选转换为 UTC 范围。峰谷档位边界按 UTC+0 分钟存储,电脑时区变化只改变显示。订阅起始日是 UTC 日期锚点,前端使用可逆的当地日期映射,避免换时区后原样保存改动账期。
- **计费写时固化。** 改价、换序不回溯 `request_log.api_cost`,见 [billing-pricing.md](billing-pricing.md)。
- **request_log 明细不上传。** 普通用户配置和本地代理密钥上传到配置云端文件；上游 API Key 明文与 WebDAV 密码只保存在本机。用量和账单分别使用 `sync_state.last_exported_log_id` 与 `sync_state.last_exported_billing_event_id`，拉取-导出-上传是完整事务(失败回滚),见 [sync.md](sync.md)。
- **Python 无依赖清单。** 只有 flask、requests 两个第三方包,启动脚本在缺 flask 时自动 pip 安装。
- **数据库路径与 schema 目录的推导约定**:`data/token-board.db` /
  `data/dashboard.db` → `<仓库>/schema/`；Python coordinator 再选择
  `token-board|dashboard/v<major>`。C++ 只校验已准备好的 Proxy V2 数据库。
- **代码修改期间防御**：在修改代码时，需要提前停止开机自启动的服务，避免代码修改到一半时开关机导致未完工代码上线运行。
