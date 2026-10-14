# BB 贴图优化 Skill

适用于 Blockbench 重复贴图、UV 去重和 2048×2048 到 128×128 图集优化。输出可编辑模型副本、独立 PNG、区域记录和视觉对比。

包含两条路线：原尺寸精确去重，以及明确配置材质组后的比例 UV 共享。保护文字、眼睛等关键区域，校验几何、层级和动画；不承诺所有大图都能无损缩到 128。

## 使用

将本仓库文件夹保存为 `bb-texture-optimization`，放入你的技能目录，然后使用：

```text
使用 $bb-texture-optimization 优化这个 .bbmodel 的贴图，目标 128×128，保持原风格并输出模型副本和 PNG。
```

[技能入口](SKILL.md) · [详细策略](references/workflow.md) · [工具参数与配置](references/helper.md)

脚本依赖 Python 3.10+，安装依赖后先审计：

```bash
python -m pip install -r requirements.txt
python -B scripts/bb_texture_optimize.py audit model.bbmodel --out audit
python -B scripts/bb_texture_optimize.py optimize model.bbmodel --out optimized --size 128 --mode exact
```

共享模式需要经视觉检查的显式配置。助手支持静态颜色贴图、逐面 cube UV；Box UV、mesh、PBR、活动图层与动画贴图使用原生工作流程。

## 验证

```bash
python -B -m unittest discover -s tests -v
```

测试覆盖重复内容、逻辑 UV 与图片尺寸、分数坐标、镜像、旋转、透明度、容量不足、保护区域、相对贴图引用、原文件与动画保护以及不支持格式的拒绝。软件比较图不能替代原生/游戏验证。

仓库包含通用技能、源码、合成测试与依赖声明，不含具体项目模型或用户凭据；处理脚本不依赖固定本地目录。
