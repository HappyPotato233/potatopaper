# 结构对齐校验（只读，不执行任何 DDL）：information_schema vs 模型 metadata
# 验收标准：差异清单为空。差异由人工判断处理。
import argparse
import sys
from pathlib import Path

# 允许以脚本方式直接运行（python scripts/align_schema.py）：把项目根加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect

from app.config import settings
from app.models import Base
from app.session import make_url

# 类型等价表：模型生成的类型与 MySQL 存储类型之间的归一化
# 实测补充：
#  - Boolean 在 MySQL 生成 BOOL，而 MySQL 8 的 BOOL 与 TINYINT(1) 同义，反射只回显 TINYINT（无显示宽度）；
#  - 库反射的字符串类型自带 COLLATE 后缀、ENUM 只回显关键字（不含枚举值），统一在 _norm_type 里剥离归一。
TYPE_EQUIV = {
    "bool": "tinyint(1)",
    "boolean": "tinyint(1)",
    "tinyint": "tinyint(1)",
    "tinyint(1)": "tinyint(1)",
}

# ON UPDATE 标记：information_schema.extra 与模型 server_default 的比对依据
TS_UPDATE_MARK = "ON UPDATE CURRENT_TIMESTAMP"


def _norm_type(t: str) -> str:
    """归一化列类型字符串：小写、去空白/去 COLLATE/去引号，BOOL→tinyint(1)，ENUM 只留关键字"""
    s = "".join(str(t).lower().split())
    s = s.split("collate")[0]              # 剥离反射类型自带的排序规则后缀
    s = s.replace('"', "").replace("`", "")
    s = s.replace("unsigned", "")
    if s.startswith("enum"):               # 库反射 ENUM 不含枚举值，统一归一到关键字
        s = "enum"
    return TYPE_EQUIV.get(s, s)


def _norm_default(d) -> str | None:
    """归一化默认值：None 与 'NULL' 等价"""
    if d is None:
        return None
    s = str(d).strip()
    if s.upper() in ("NULL", ""):
        return None
    # MySQL 对 CURRENT_TIMESTAMP 的回显可能与写入形式不同，去引号后比较
    return s.strip("'")


def _meta_type(col) -> str:
    """模型列类型 → MySQL 方言字符串"""
    from sqlalchemy.dialects.mysql import dialect as mysql_dialect
    return str(col.type.compile(dialect=mysql_dialect()))


def _meta_default(col) -> str | None:
    """模型列 server_default → 字符串"""
    sd = col.server_default
    if sd is None:
        return None
    return _norm_default(getattr(sd, "arg", None))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="库名，默认 settings.MYSQL_DATABASE")
    ap.add_argument("--show-ddl", action="store_true", help="额外打印 SHOW CREATE TABLE（人工核对 ngram 解析器）")
    args = ap.parse_args()

    eng = create_engine(make_url(args.db or settings.MYSQL_DATABASE))
    insp = inspect(eng)
    db_tables = set(insp.get_table_names())
    diffs: list[str] = []

    for table in Base.metadata.sorted_tables:
        name = table.name
        if name not in db_tables:
            diffs.append(f"[表缺失] {name}")
            continue

        # 1) 列
        db_cols = {c["name"]: c for c in insp.get_columns(name)}
        meta_cols = {c.name: c for c in table.columns}
        for cname in sorted(set(meta_cols) | set(db_cols)):
            if cname not in db_cols:
                diffs.append(f"[列缺失] {name}.{cname}")
                continue
            if cname not in meta_cols:
                diffs.append(f"[列多余] {name}.{cname}")
                continue
            m, d = meta_cols[cname], db_cols[cname]
            if _norm_type(_meta_type(m)) != _norm_type(d["type"]):
                diffs.append(f"[类型] {name}.{cname}: 模型 {_meta_type(m)} ≠ 库 {d['type']}")
            if bool(m.nullable) != bool(d["nullable"]):
                diffs.append(f"[可空] {name}.{cname}: 模型 {m.nullable} ≠ 库 {d['nullable']}")
            if _meta_default(m) != _norm_default(d.get("default")):
                diffs.append(f"[默认值] {name}.{cname}: 模型 {_meta_default(m)} ≠ 库 {_norm_default(d.get('default'))}")
            extra = (d.get("extra") or "")
            if "on update CURRENT_TIMESTAMP" in extra.upper() and TS_UPDATE_MARK not in str(m.server_default.arg if m.server_default else ""):
                diffs.append(f"[ON UPDATE] {name}.{cname}: 库有 ON UPDATE，模型未声明")
            if "auto_increment" in extra.lower() and not m.autoincrement:
                diffs.append(f"[自增] {name}.{cname}: 库自增，模型未声明")

        # 2) 索引 / 唯一键
        meta_idx: dict[str, tuple[list[str], bool]] = {}
        for ix in table.indexes:                      # 显式 Index
            meta_idx[ix.name] = ([c.name for c in ix.columns], bool(ix.unique))
        for con in table.constraints:                 # 具名 UniqueConstraint
            from sqlalchemy import UniqueConstraint
            if isinstance(con, UniqueConstraint) and con.name:
                meta_idx[con.name] = ([c.name for c in con.columns], True)
        # 内联 unique（如 users.username / settings.user_id）在库中索引名 = 列名
        for c in table.columns:
            if c.unique:
                meta_idx.setdefault(c.name, ([c.name], True))

        # 外键约束名：MySQL 会为缺少覆盖索引的外键自动创建同名支撑索引（模型无需声明），不参与比对
        fk_names = {fk.name for fk in table.foreign_key_constraints}
        db_idx = {i["name"]: i for i in insp.get_indexes(name)
                  if i["name"] in meta_idx or i["name"] not in fk_names}

        for iname in sorted(set(meta_idx) | set(db_idx)):
            if iname not in db_idx:
                diffs.append(f"[索引缺失] {name}.{iname}（模型列 {meta_idx.get(iname, ('?',))[0]}）")
                continue
            if iname not in meta_idx:
                diffs.append(f"[索引多余] {name}.{iname}")
                continue
            dcols, duniq = meta_idx[iname]
            if db_idx[iname].get("column_names") != dcols:
                diffs.append(f"[索引列] {name}.{iname}: 模型 {dcols} ≠ 库 {db_idx[iname].get('column_names')}")
            if bool(db_idx[iname].get("unique")) != duniq:
                diffs.append(f"[索引唯一性] {name}.{iname}: 模型 {duniq} ≠ 库 {db_idx[iname].get('unique')}")
            if iname == "ft_papers_text":
                # FULLTEXT 解析器 information_schema 不暴露，改为 SHOW CREATE TABLE 核对
                # 库中回显为 WITH PARSER `ngram`（带反引号），故先去掉反引号再比对
                ddl = eng.connect().exec_driver_sql(f"SHOW CREATE TABLE `{name}`").fetchone()[1]
                if "with parser ngram" not in ddl.lower().replace("`", ""):
                    diffs.append(f"[全文解析器] {name}.{iname}: 库中未使用 ngram 解析器")

        # 3) 外键
        db_fk = {fk.get("name"): fk for fk in insp.get_foreign_keys(name)}
        meta_fk: dict[str, tuple[list[str], str, list[str], str]] = {}
        for fk in table.foreign_key_constraints:
            elem = list(fk.elements)[0]
            meta_fk[fk.name] = ([e.parent.name for e in fk.elements], elem.column.table.name,
                                [e.column.name for e in fk.elements],
                                (fk.ondelete or "").upper())
        for fname in sorted(set(meta_fk) | set(db_fk)):
            if fname not in db_fk:
                diffs.append(f"[外键缺失] {name}.{fname}")
                continue
            if fname not in meta_fk:
                diffs.append(f"[外键多余] {name}.{fname}")
                continue
            dcols, dref_t, dref_c, dondel = meta_fk[fname]
            d = db_fk[fname]
            if d.get("constrained_columns") != dcols:
                diffs.append(f"[外键列] {name}.{fname}: 模型 {dcols} ≠ 库 {d.get('constrained_columns')}")
            if d.get("referred_table") != dref_t:
                diffs.append(f"[外键目标表] {name}.{fname}: 模型 {dref_t} ≠ 库 {d.get('referred_table')}")
            if d.get("referred_columns") != dref_c:
                diffs.append(f"[外键目标列] {name}.{fname}: 模型 {dref_c} ≠ 库 {d.get('referred_columns')}")
            if (d.get("options", {}).get("ondelete") or "").upper() != dondel:
                diffs.append(f"[外键级联] {name}.{fname}: 模型 {dondel} ≠ 库 {(d.get('options') or {}).get('ondelete')}")

        if args.show_ddl:
            print(eng.connect().exec_driver_sql(f"SHOW CREATE TABLE `{name}`").fetchone()[1], "\n")

    eng.dispose()
    if diffs:
        print(f"发现 {len(diffs)} 处差异：")
        for item in diffs:
            print("  -", item)
        return 1
    print("结构对齐校验通过：差异清单为空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
