from setuptools import setup
from Cython.Build import cythonize
from pathlib import Path

targets = [str(p) for p in Path("app").rglob("*.py") if p.name != "__init__.py"]

setup(
    name="seacraft-asr",
    ext_modules=cythonize(
        targets,
        compiler_directives={
            "language_level": 3,
            "always_allow_keywords": True,       # FastAPI/Pydantic 依赖关键字参数
            "annotation_typing": False,          # 关掉注解→类型推断（FastAPI 的 Form/File/Depends 作默认值需要）
            "emit_code_comments": False,         # 不在 .c 里嵌入原源码片段，减少反推风险
        },
        build_dir="build",
    ),
)