# 文件助手

依赖 Python 3.10+、Pillow、NumPy。运行 `python scripts/bb_texture_optimize.py --help` 查看完整参数。所有输入按数据处理，不执行模型或原绘制脚本。

## 审计

```powershell
python -B scripts/bb_texture_optimize.py audit "G:/ChatGPT/project/model.bbmodel" --out "G:/ChatGPT/project/audit"
```

生成 `audit.json`（图片/逻辑 UV 尺寸、面像素范围、表面长度、密度、精确独有内容）及 `face_assignments.json`。该配置模板默认全部保护；先查看贴图/模型，再修改适合共享的面。

## 精确去重

```powershell
python -B scripts/bb_texture_optimize.py optimize "G:/ChatGPT/project/model.bbmodel" --out "G:/ChatGPT/project/exact_128" --size 128 --mode exact
```

不改变基准层最近邻 texel 样本。保留亚像素裁剪偏移、镜像及面旋转。若装不下则以非零状态退出，不写出优化模型或 PNG；不会自动切换有损策略。

## 显式共享配置

下面使用一个示例 UUID；实际值必须来自审计。逐面配置优先于整个元素配置，未指定的面默认精确保留。

```json
{
  "groups": {
    "armor": {"strategy": "field", "variants": 2, "trim_pixels": 1},
    "label": {"strategy": "patch", "source": "label.png"},
    "protected": {"strategy": "exact"}
  },
  "elements": {"11111111-2222-4333-8444-555555555555": "armor"},
  "faces": {
    "11111111-2222-4333-8444-555555555555:east": "label",
    "11111111-2222-4333-8444-555555555555:west": "protected"
  }
}
```

```powershell
python -B scripts/bb_texture_optimize.py optimize "G:/ChatGPT/project/model.bbmodel" --out "G:/ChatGPT/project/shared_128" --size 128 --mode shared --config "G:/ChatGPT/project/groups.json" --density 1.2
```

| strategy | 行为 |
|---|---|
| exact | 原尺寸 RGBA 精确去重 |
| field | 从该材质的代表图案构造有界材质场，按表面长度和统一密度读取比例子区域 |
| patch | 同尺寸内容选实际来源代表；可指定 scale；或读取明确提供的 source PNG |

`field` 可设置 `variants`、`density` 和 `trim_pixels`。若提供外部场图，还要设置 `source_density: [x,y]` 表示来源图片每模型单位的像素密度。来源不一定无缝，需在模型上检查代表图案的重复和边缘。

`patch` 默认 scale 为 1。缩小或替换特征贴图属于明确的重绘/简化，不是精确模式；外部 source 按整张小图映射，原方向和旋转保留。检查文字和标志的比例，避免把不匹配的图案拉伸。相对 source 路径相对于配置文件。

`--density auto` 尝试候选密度寻找可装入的方案，仅代表装箱建议，**不代表视觉达标**。优先给出通过视觉审核的数值；为保护区域设置最低可读像素，并在比较后决定是否接受。字段中的密度按表面长度计算，不等同于图集尺寸。

## 限制和验证

助手支持静态颜色贴图的 cube 逐面 UV。Box UV 需先在原生副本中转换并检查；mesh、活动图层、动画贴图、PBR、关联贴图组和不同材质采样设置需要原生流程。未显式编码的动画条带不能靠脚本猜测，调用者必须先确认单帧纹理。自定义 UV 规则可用 `--uv-size width height` 指定实际逻辑尺寸，不要猜测。

输出包含模型副本、独立并嵌入的 PNG、区域/面对应表和验证报告。已有输出拒绝覆盖，换一个新目录。报告中的 visual_validation 默认 pending；它不能证明原生打开、动画播放或游戏过滤已通过。

```powershell
python -B scripts/render_compare.py "G:/ChatGPT/project/model.bbmodel" "G:/ChatGPT/project/shared_128/model_optimized_128.bbmodel" --out "G:/ChatGPT/project/comparison"
```

软件渲染只支持不透明且几何相同的逐面 cube 模型，使用静态姿态和简化光照。比较通过后仍应在可用的目标环境验证。遵守调用者的文件存储规则，命令中的 G: 是示例。

软件助手会拒绝含透明 UV 区域的比较；透明度、混合排序及剪裁使用原生预览。审计摘要报告模式、通道及优化支持状态；审计和优化都拒绝覆盖已有输出。
