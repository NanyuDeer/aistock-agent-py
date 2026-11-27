# Brain V4 受限执行包模板

> 本文件是模板，不是可直接执行的任务。签发人必须删除所有尖括号占位内容，填入准确路径、契约、测试和命令后才能交给开发者或模型。

## 1. Packet Metadata

~~~yaml
packet_id: <V4-XX-PYY>
objective: <一个可评审行为，不得包含两个独立目标>
owner: <name-or-team>
prerequisite_repo: <aistock-agent-py|aistock-app-api>
prerequisite_commit: <40-char-sha>
contract_baseline: <v4.x>
feature_id: <feature-or-platform-component>
expected_commit_message: <type(scope): summary>
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - <exact/file/path>
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/graph/state.py
  - src/aistock_agent/brain_v4/graph/builder.py
  - src/aistock_agent/brain_v4/generated/registry.lock
  - <task-specific-forbidden-path>
max_files_changed: <integer>
~~~

规则：实际修改文件必须是 `allowed_files` 的子集。需要新增文件时，签发人先更新执行包；执行者不能自行扩大范围。生成文件只能通过指定生成命令更新。

## 3. Contracts

**Consumes:**

- `<module.Type@version>`: <只读字段和语义>

**Produces:**

- `<module.Type@version>`: <字段、错误态和后续调用者>

**Invariants:**

- `as_of`、`snapshot_id` 和 `snapshot_sha` 的要求。
- active/shadow owner 和写入边界。
- 正常、`partial`、`insufficient`、timeout 的准确行为。
- 禁止从 legacy narrative 生成 V4 evidence。

## 4. Required Context

只允许执行者读取以下上下文；不要让较弱模型自行遍历整个仓库：

1. `<spec section or exact file:line>`
2. `<interface source file>`
3. `<nearest existing pattern file>`
4. `<fixture path>`

## 5. Failing Test First

**Test file:** `<exact test path>`

~~~python
def test_exact_behavior():
    result = target_call(frozen_input())
    assert result == expected_value
~~~

**Run:**

~~~text
<exact command>
~~~

**Expected failure before implementation:** `<exact exception, assertion or missing symbol>`

## 6. Implementation Steps

每一步只做一个动作，完成后再进入下一步：

1. 在 `<file>` 定义 `<exact signature>`。
2. 在 `<file>` 实现 `<exact deterministic rule>`。
3. 在 `<file>` 连接 `<existing interface>`，不得增加新依赖。
4. 运行目标测试并修正本任务范围内的失败。
5. 运行范围检查、契约测试和受影响回放。

禁止事项：

- 不修改未列入 allowlist 的文件。
- 不重命名公共类型、字段、目录或 route。
- 不增加公共抽象、依赖、环境变量或数据库字段。
- 不顺手格式化、清理或重构无关代码。
- 不把旧链结论作为 V4 evidence 或测试期望。
- 不降低、删除或跳过既有测试来获得通过。

## 7. Verification

按顺序运行，任何一步失败都不得提交：

~~~text
<target test command>
<brain-v4 contract-suite command>
<affected replay command>
<ruff/typecheck/build command>
<verify_change_scope command with this packet>
~~~

## 8. Completion Evidence

执行者最终只提交以下证据：

~~~yaml
changed_files: []
tests:
  - command: <exact command>
    result: <pass/fail and counts>
contract_versions: []
fixture_or_snapshot_sha: <sha-or-not-applicable>
generated_artifacts: []
known_limitations: []
unresolved_questions: []
~~~

## 9. Stop Conditions

出现以下任一情况必须停止并请求签发人修改执行包，不能猜测：

- 所需文件不在 allowlist。
- 实际接口与执行包中的签名不一致。
- 需要修改红区或数据库 schema，但执行包未授权。
- fixture 无法复现预期失败。
- 相同 snapshot 产生不一致输入。
- 目标测试通过但要求行为尚未实现，说明测试或计划错误。
- 发现用户或其他开发者的未提交修改与任务冲突。

## 10. Model Invocation

交给实现模型时只附上：本执行包、Required Context 中列出的文件、当前 diff 和测试输出。固定指令如下：

> 严格执行本 Packet。先验证 prerequisite commit 和 scope，再写指定失败测试。只修改 allowed_files，不做额外设计或重构。任何 Stop Condition 出现时停止并报告，不自行扩大范围。完成后返回 Completion Evidence，不宣布未运行的检查通过。
