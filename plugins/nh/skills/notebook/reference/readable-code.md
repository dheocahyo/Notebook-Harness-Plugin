# Readable, simple code: 8 before/after pairs

The user reads every cell and must be able to explain it. Prefer code a junior
data scientist can follow line by line over code that is short. nh's
readability hints (L101-L123) point at the same problems; a hint arrives
after the run, so offer "tidy" rather than rewriting unasked.

## 1. Name intermediate results instead of nesting calls

Before:
```python
df[df["price"] > df.groupby("region")["price"].transform("median") * 3]
```

After:
```python
region_median = df.groupby("region")["price"].transform("median")
is_expensive = df["price"] > region_median * 3
expensive_orders = df[is_expensive]
expensive_orders[["region", "product", "price"]]
```
Each line holds one idea, and the user can print any name to check it.

## 2. Keep method chains to 4 links or fewer

Before:
```python
top_regions = (
    df.dropna(subset=["price"])
    .assign(revenue=lambda d: d["units"] * d["price"])
    .groupby("region")["revenue"]
    .sum()
    .sort_values(ascending=False)
    .head(3)
    .reset_index()
    .rename(columns={"revenue": "total_revenue"})
)
```

After:
```python
df_priced = df.dropna(subset=["price"])
df_revenue = df_priced.assign(revenue=df_priced["units"] * df_priced["price"])
revenue_by_region = df_revenue.groupby("region")["revenue"].sum()
revenue_by_region.sort_values(ascending=False).head(3)
```
A long chain can't be inspected halfway. Break it where the data changes shape.

## 3. New names for transformed data; no `inplace=True`

Before (prices arrive in cents):
```python
df.dropna(subset=["price"], inplace=True)
df["price"] = df["price"] / 100
```

After:
```python
CENTS_PER_UNIT = 100

df_priced = df.dropna(subset=["price"])
df_clean = df_priced.assign(price=df_priced["price"] / CENTS_PER_UNIT)
df_clean["price"].describe()
```
Running the "before" cell twice divides prices by 100 twice. Named results make
every cell safe to re-run, and `df` stays as loaded.

## 4. UPPER_CASE constants for judgment calls

Before:
```python
df_trimmed = df[(df["price"] < 5000) & (df["units"] > 0)]
```

After:
```python
PRICE_CAP = 5_000  # above this, prices in this source are data-entry errors
MIN_UNITS = 1

is_plausible = (df["price"] < PRICE_CAP) & (df["units"] >= MIN_UNITS)
df_trimmed = df[is_plausible]
print(f"kept {len(df_trimmed)} of {len(df)} rows")
```
The constants are the decisions the user may want to change. Name them in the
reply.

## 5. Column arithmetic instead of `apply(lambda …, axis=1)`

Before:
```python
df["revenue"] = df.apply(lambda row: row["units"] * row["price"] if row["price"] > 0 else 0, axis=1)
```

After:
```python
revenue = df["units"] * df["price"]
df_sales = df.assign(revenue=revenue.where(df["price"] > 0, 0))
df_sales[["units", "price", "revenue"]].head()
```
Row-wise `apply` hides the rule inside a lambda and is slow on large frames.

## 6. No functions until the code is reused

Before:
```python
def summarize(frame, col):
    stats = frame.groupby(col)["price"].agg(["count", "mean", "median"])
    return stats.sort_values("mean", ascending=False)


summarize(df, "region")
```

After:
```python
price_by_region = df.groupby("region")["price"].agg(["count", "mean", "median"])
price_by_region.sort_values("mean", ascending=False)
```
A function used once is one more thing to read. Write it when the second use
arrives, and say so in the reply.

## 7. End with something visible; no prose printed from code

Before:
```python
df_clean = df.dropna(subset=["price"])
print(
    "We removed the rows that had missing prices because they cannot be used to compute averages."
)
```

After:
```python
df_clean = df.dropna(subset=["price"])
print(f"rows: {len(df)} -> {len(df_clean)}")
df_clean.head()
```
The why goes in the note and the chat. The cell ends with something the user
can check: a shape, a head, a small table, or one plot with a title and axis
labels.

## 8. Let problems show instead of hiding them

Before:
```python
import warnings

warnings.filterwarnings("ignore")

try:
    df["order_date"] = pd.to_datetime(df["order_date"])
except:
    pass
```

After:
```python
order_date = pd.to_datetime(df["order_date"], errors="coerce")
unparsed = df.loc[order_date.isna(), "order_date"]
df_dated = df.assign(order_date=order_date)
unparsed
```
Silenced warnings and a bare `except: pass` hide exactly what the user needs to
see. Handle the specific case and show what it affected.
