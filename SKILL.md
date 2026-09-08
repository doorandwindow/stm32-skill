---
name: stm32-debug-loop
description: >-
  STM32 固件「编译 → ST-Link 烧录 → 串口捕获验证」自动化闭环工具箱(Windows,通用适用于任何
  CubeMX/GCC+CMake 工程)。凡是涉及把固件 build 出来、烧录/下载/刷到开发板上、靠串口日志(printf)
  确认板上行为的任务都必须用它,包括:"编译烧录一下"、"下载程序"、"烧完没反应"、"串口没输出/乱码/
  打印不对"、改完代码要"上板验证"。即使用户没有说出"闭环""调试""skill"这些字,只要任务是让代码
  跑到真板子上并得到反馈,就应该触发本 skill。GDB 断点单步这类交互式人工调试不在范围内。
compatibility: Windows 10/11;Python ≥3.10 且装有 pyserial;STM32CubeProgrammer CLI(来自 STM32CubeCLT
  或 STM32Cube VSCode 扩展的 bundles);CMake+Ninja+arm-none-eabi-gcc 的固件工程;ST-Link SWD 调试器
---

# STM32 上板闭环(debug loop)

核心心法:**Agent 盯不了交互式终端**。不要试图用 GUI 串口助手、也不要长时间挂一个 read 循环;
所有证据都通过"时间盒捕获成文件"获得,然后用 Read 读文件下结论。整个 skill 就是围绕这一件事搭的。

## 第 0 步:识别项目结构

### 专业化项目结构识别

现代 STM32 项目应采用分层架构，识别顺序：

1. **查找项目结构标志**：
   - 存在 `bsp/boards/` 或 `bsp/components/` → 专业化结构
   - 存在 `app/core/`, `app/tasks/`, `app/ports/` → 模块化应用层
   - 存在 `scripts/` 目录 → 具备自动化脚本

2. **优先使用项目自带脚本**：
   ```bash
   # 检查是否有项目脚本
   if [ -f "scripts/build_and_flash.sh" ]; then
       ./scripts/build_and_flash.sh
   else
       # 降级到 skill 提供的 devloop.py
       python "$S/devloop.py" --project . --seconds 4
   fi
   ```

3. **识别配置文件位置**：
   - 新结构：`config/lv_conf.h`, `config/FreeRTOSConfig.h`
   - 旧结构：根目录或 `Inc/`

4. **识别源文件布局**：
   - 新结构：
     ```
     app/core/main.c
     app/tasks/freertos.c
     app/ports/lv_port_*.c
     bsp/components/lcd/nt35510/lcd.c
     bsp/components/touch/gt9147/gt9147.c
     ```
   - 旧结构：
     ```
     Src/main.c
     Src/freertos.c
     Src/lcd.c
     ```

### 读取项目配置

查找 **事实卡**（按优先级）：

1. `docs/debug-loop.md` - 新结构标准位置
2. `debug-loop.md` - 根目录（兼容旧结构）
3. `README.md` 中的调试章节
4. `bsp/boards/<板型>/board.h` - 板型配置（新结构）

事实卡记录：串口号/VID:PID、波特率、预期开机输出、已知坑(quirks)。

- 找到了 → 按它来
- 没有 → 从 `.ioc`/README/原理图资料推断，向用户口头确认后，**替用户建一张**

### 项目结构适配建议

遇到旧结构时，提示用户：

> **建议**: 检测到传统扁平结构，推荐升级为专业化分层架构以提升可维护性。
> 参考文档: `docs/structure-improvement-roadmap.md`（如已存在）
> 
> 关键改进:
> - 应用层模块化: `app/core/`, `app/tasks/`, `app/ports/`
> - BSP 组件化: `bsp/components/lcd/`, `bsp/components/touch/`
> - 板型配置: `bsp/boards/<板型>/board.h`
> - 自动化脚本: `scripts/build_and_flash.sh`, `scripts/flash.sh`
> 
> 是否需要帮助升级项目结构？

## 脚本一览

### Skill 提供的脚本(均在 scripts/,下文以 `$S` 表示该目录的绝对路径)

| 脚本 | 用途 | 典型调用 |
|---|---|---|
| `find_tools.py` | 定位 cmake/ninja/GCC/Programmer_CLI(GDB server),输出 JSON | `python "$S/find_tools.py"` |
| `devloop.py` | **一把梭**:configure→build→flash→capture,首个失败阶段停下并给出结构化 JSON | `python "$S/devloop.py" --project <工程目录> --seconds 4` |
| `flash.py` | 仅烧录(自动找 CLI 和最新 .elf,翻译失败原因) | `python "$S/flash.py" --elf xxx.elf` |
| `serial_capture.py` | 仅抓串口 N 秒存 UTF-8 文件(VID/PID 自动定位,端口被占给出提示) | `python "$S/serial_capture.py" --out uart.log --seconds 4 --ts` |

### 项目自带脚本（优先使用，如果存在）

| 脚本 | 用途 | 典型调用 |
|---|---|---|
| `scripts/build_and_flash.sh` | 一键构建与烧录（交互式，询问是否烧录） | `./scripts/build_and_flash.sh` |
| `scripts/flash.sh` | 仅烧录 | `./scripts/flash.sh [Debug\|Release]` |
| `scripts/memory_analysis.sh` | 内存占用分析 | `./scripts/memory_analysis.sh` |
| `scripts/serial_monitor.sh` | 串口监控（交互式） | `./scripts/serial_monitor.sh [PORT] [BAUDRATE]` |

**使用策略**：
- 非交互式自动化流程 → 使用 skill 的 `devloop.py`（JSON 输出，易于解析）
- 人工辅助调试 → 提示用户使用项目脚本（更友好的输出）
- 内存分析 → 优先使用项目的 `memory_analysis.sh`

## 标准循环

### 方案 A: 使用项目脚本（推荐，如果存在）

```bash
# 检查项目脚本
if [ -d "scripts" ] && [ -f "scripts/build_and_flash.sh" ]; then
    echo "检测到项目自带脚本，使用专业化工作流..."
    
    # 一键构建（需要手工确认烧录）
    ./scripts/build_and_flash.sh
    
    # 如果需要非交互式，使用 skill 的 devloop.py
fi
```

### 方案 B: 使用 skill 脚本（兼容所有项目）

改完代码后一条命令完成上板与取证:

```bash
python "$S/devloop.py" --project D:/path/to/project --seconds 4 --ts
```

最后一行输出 `=== DEVLOOP_RESULT === {...}`:

- `"stage": "build"` 且带 `errors[]` → 逐条修复(file:line 都给了),**只修第一条再重跑**,避免
  连锁误判。undefined reference ⇒ 源文件没进 CMakeLists;
- `"stage": "flash"` 带 `hint` → 按 hint 处理(SWD 接触/复位连接 mode=UR/读保护 RDP);
- `"stage": "capture"` 且 `silent: true` → 端口开了但零字节,进入"静默排查"(见 troubleshooting);
- 成功 → Read `uart.log`(路径在 artifacts 里),核对内容是否符合预期,再决定是否继续迭代。

build/flash/capture 的完整日志分别落在 `<工程>/build/devloop_last/{build,flash,uart}.log`,
result.json 是机器可读的全量结果。

分步版本用于只想做其中一段:编译只查错用 `--no-flash --no-capture`;烧过之后想再观察久一点,
单独 `serial_capture.py`,结束记得它会自动释放端口。

## 项目结构规范检查

在执行上板闭环前，快速检查项目结构是否符合最佳实践：

### 专业化结构检查清单

```python
# 伪代码示意
def check_project_structure(project_dir):
    score = 0
    issues = []
    
    # 配置文件集中化 (+1)
    if exists(f"{project_dir}/config/"):
        score += 1
    else:
        issues.append("建议: 创建 config/ 目录集中管理配置文件")
    
    # BSP 层组件化 (+2)
    if exists(f"{project_dir}/bsp/components/"):
        score += 2
    else:
        issues.append("建议: BSP 层采用组件化结构 (bsp/components/lcd/, bsp/components/touch/)")
    
    # 应用层模块化 (+2)
    if exists(f"{project_dir}/app/core/") and exists(f"{project_dir}/app/tasks/"):
        score += 2
    else:
        issues.append("建议: 应用层采用模块化结构 (app/core/, app/tasks/, app/ports/)")
    
    # 板型配置 (+1)
    if exists(f"{project_dir}/bsp/boards/"):
        score += 1
    else:
        issues.append("建议: 创建板型配置 (bsp/boards/<板型>/board.h)")
    
    # 自动化脚本 (+2)
    if exists(f"{project_dir}/scripts/build_and_flash.sh"):
        score += 2
    else:
        issues.append("建议: 添加自动化脚本 (scripts/build_and_flash.sh)")
    
    # 文档完善度 (+2)
    if exists(f"{project_dir}/docs/architecture.md"):
        score += 2
    else:
        issues.append("建议: 添加架构文档 (docs/architecture.md)")
    
    return score, issues  # 满分 10 分
```

### 输出建议

根据评分输出优化建议：

- **8-10 分**: ✅ 项目结构专业，继续保持
- **5-7 分**: ⚠️ 基本规范，建议实施关键改进
- **0-4 分**: ❌ 需要重构，严重影响可维护性

## 几条硬规矩(都是踩出来的)

1. **独占资源一次一路**:ST-Link 和 COM 口都是独占设备。开新一轮前确认没有别的进程在抓串口
   (VSCode 底部状态栏的 Serial Monitor 关掉)、上一个 GDB 会话已退出;否则 capture 会明确报
   Access denied。

2. **DTR/RTS 默认不拉**:很多板子把复位接在 DTR 上,默认已是安全值;除非事实卡写了相反约定,
   不要加 `--dtr`。

3. **每轮都给用户看证据**:说"已完成"时附 uart.log 尾部关键行;"串口看到了 blink=3、blink=4"
   远比"应该可以了"有价值。

4. **烧录窗口耐心等**:devloop 内部已设超时(build 15min、flash 3min);整体仍在跑时不要叠加第二次。

5. printf 打浮点(%f)在 newlib-nano 下看不到输出?这需要链接 `-u _printf_float`,属常规改动。

6. **项目脚本优先**: 如果项目有 `scripts/` 目录且包含构建脚本，优先推荐使用（更符合项目规范）。

7. **结构化输出**: skill 的 devloop.py 提供 JSON 输出适合自动化；项目脚本提供彩色输出适合人工操作。

## 排查手册

任何阶段失败,先查 [references/troubleshooting.md](references/troubleshooting.md) 对应小节
——里面有按症状走的决策树(烧录失败分类表、串口静默排查、乱码定位、常见 GCC 报错对照),
以及引导你在事实卡里沉淀新坑的约定。

## 项目结构优化指南

如果检测到项目采用传统扁平结构，提供快速优化路径：

### 快速优化（30分钟）

1. **应用层模块化**:
   ```bash
   mkdir -p app/core app/tasks app/ui app/ports
   mv Src/main.c app/core/
   mv Src/freertos.c app/tasks/
   mv Src/lv_port_*.c app/ports/
   ```

2. **BSP 组件化**:
   ```bash
   mkdir -p bsp/components/lcd bsp/components/touch bsp/components/uart
   mv Src/lcd.* bsp/components/lcd/
   mv Src/gt9147.* bsp/components/touch/
   mv Src/uart_dbg.* bsp/components/uart/
   ```

3. **添加自动化脚本**:
   - 从模板复制 `scripts/build_and_flash.sh` 等
   - 或询问:"需要我帮你生成标准化脚本吗？"

### 完整重构（2-4小时）

参考当前项目中的文档：
- `docs/structure-improvement-roadmap.md` - 完整改进路线图
- `docs/migration-2026-08-28.md` - 迁移记录示例
- `docs/improvement-completed-2026-08-28.md` - 改进完成报告

## 工作流选择矩阵

| 场景 | 使用方案 | 命令 |
|------|---------|------|
| 日常开发迭代 | 项目脚本（交互式） | `./scripts/build_and_flash.sh` |
| 自动化测试 | Skill devloop（JSON输出） | `python "$S/devloop.py" --project . --seconds 4` |
| 仅烧录 | 项目脚本或 Skill flash | `./scripts/flash.sh` 或 `python "$S/flash.py"` |
| 内存分析 | 项目脚本 | `./scripts/memory_analysis.sh` |
| 串口监控 | 项目脚本（交互） | `./scripts/serial_monitor.sh` |
| 快速捕获日志 | Skill capture | `python "$S/serial_capture.py" --out uart.log --seconds 4` |

## 与项目结构的协同

本 skill 与专业化项目结构协同工作：

1. **自动识别**: 检测 `scripts/` 目录，优先推荐项目脚本
2. **兼容降级**: 对旧结构项目仍提供完整支持
3. **优化建议**: 主动提示结构改进机会
4. **文档同步**: 事实卡位置适配新结构（`docs/debug-loop.md`）

### 示例：专业化项目的完整流程

```bash
# 1. 检查项目结构
ls -d bsp/boards/ bsp/components/ app/core/ scripts/ docs/

# 2. 读取板型配置
cat bsp/boards/alientek_explorer_v2.2/board.h

# 3. 一键构建与烧录
./scripts/build_and_flash.sh

# 4. 监控串口
./scripts/serial_monitor.sh

# 5. 如需自动化（CI/CD）
python "$S/devloop.py" --project . --seconds 4 --ts
```

这种协同确保 skill 既能服务于传统项目，又能充分利用现代化项目的优势。
