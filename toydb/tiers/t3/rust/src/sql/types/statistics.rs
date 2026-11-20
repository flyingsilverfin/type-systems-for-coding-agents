use std::collections::{BTreeMap, HashMap};
use std::fmt::Display;

use serde::{Deserialize, Serialize};

use super::{DataType, Rows, Table, Value};
use crate::encoding;
use crate::error::Result;

/// Column statistics for a table, computed by ANALYZE and stored in the
/// catalog. They describe the table as it was when it was analyzed, and are
/// not updated by later writes.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TableStatistics {
    /// The number of table rows.
    pub rows: u64,
    /// Statistics for each column, keyed by column name. Columns without
    /// statistics (e.g. added after the table was analyzed) are missing.
    pub columns: BTreeMap<String, ColumnStatistics>,
}

impl encoding::Value for TableStatistics {}

/// Statistics for a single column.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ColumnStatistics {
    /// The number of NULL values.
    pub nulls: u64,
    /// The number of distinct non-NULL values, as grouped by GROUP BY (i.e.
    /// using Value equality, where NaN equals NaN).
    pub distinct: u64,
    /// The smallest non-NULL value, in Value order (as used by ORDER BY), or
    /// None if there are no non-NULL values.
    pub min: Option<Value>,
    /// The largest non-NULL value, in Value order, or None if there are no
    /// non-NULL values.
    pub max: Option<Value>,
    /// The most frequent non-NULL value and its number of rows, or None if
    /// there are no non-NULL values. Ties go to the smallest value.
    pub top: Option<(Value, u64)>,
    /// The average length in characters of the non-NULL values of a STRING
    /// column. None for other column types, or without non-NULL values.
    pub avg_length: Option<f64>,
}

impl TableStatistics {
    /// Computes statistics for the given table rows.
    pub fn compute(table: &Table, rows: Rows) -> Result<Self> {
        // Count the occurrences of each value in each column.
        let mut count = 0;
        let mut values: Vec<HashMap<Value, u64>> = vec![HashMap::new(); table.columns.len()];
        let mut nulls = vec![0; table.columns.len()];
        for row in rows {
            count += 1;
            for (i, value) in row?.into_iter().enumerate() {
                match value {
                    Value::Null => nulls[i] += 1,
                    value => *values[i].entry(value).or_default() += 1,
                }
            }
        }

        let columns = table
            .columns
            .iter()
            .zip(values)
            .zip(nulls)
            .map(|((column, values), nulls)| {
                (column.name.clone(), ColumnStatistics::compute(column.datatype, values, nulls))
            })
            .collect();
        Ok(Self { rows: count, columns })
    }
}

impl ColumnStatistics {
    /// Computes column statistics from the number of occurrences of each
    /// non-NULL value and the number of NULLs.
    fn compute(datatype: DataType, values: HashMap<Value, u64>, nulls: u64) -> Self {
        let min = values.keys().min().cloned();
        let max = values.keys().max().cloned();
        let top = values
            .iter()
            .max_by(|(a, a_count), (b, b_count)| a_count.cmp(b_count).then_with(|| b.cmp(a)))
            .map(|(value, count)| (value.clone(), *count));

        // Only strings have a length.
        let non_null: u64 = values.values().sum();
        let avg_length = match datatype {
            DataType::String if non_null > 0 => {
                let chars: u64 = values
                    .iter()
                    .map(|(value, count)| match value {
                        Value::String(s) => s.chars().count() as u64 * count,
                        _ => 0,
                    })
                    .sum();
                Some(chars as f64 / non_null as f64)
            }
            _ => None,
        };

        Self { nulls, distinct: values.len() as u64, min, max, top, avg_length }
    }
}

/// Formats the statistics on a single line, e.g. for storage dumps.
impl Display for TableStatistics {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "rows={}", self.rows)?;
        for (name, column) in &self.columns {
            write!(f, " {name}({column})")?;
        }
        Ok(())
    }
}

impl Display for ColumnStatistics {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let format = |value: &Option<Value>| value.as_ref().map_or("None".into(), Value::to_string);
        write!(f, "nulls={} distinct={}", self.nulls, self.distinct)?;
        write!(f, " min={} max={}", format(&self.min), format(&self.max))?;
        match &self.top {
            Some((value, count)) => write!(f, " top={value}×{count}")?,
            None => write!(f, " top=None")?,
        }
        if let Some(avg_length) = self.avg_length {
            write!(f, " avg_length={avg_length:?}")?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sql::types::{Column, Row};

    fn table(columns: &[(&str, DataType)]) -> Table {
        Table {
            name: "t".into(),
            primary_key: 0,
            columns: columns
                .iter()
                .map(|(name, datatype)| Column {
                    name: name.to_string(),
                    datatype: *datatype,
                    nullable: true,
                    default: Some(Value::Null),
                    unique: false,
                    index: false,
                    references: None,
                })
                .collect(),
        }
    }

    fn compute(table: &Table, rows: Vec<Row>) -> TableStatistics {
        TableStatistics::compute(table, Box::new(rows.into_iter().map(Ok))).expect("compute failed")
    }

    #[test]
    fn empty() {
        let table = table(&[("id", DataType::Integer), ("s", DataType::String)]);
        let stats = compute(&table, vec![]);
        assert_eq!(stats.rows, 0);
        let s = &stats.columns["s"];
        assert_eq!((s.nulls, s.distinct), (0, 0));
        assert_eq!((&s.min, &s.max, &s.top, s.avg_length), (&None, &None, &None, None));
    }

    #[test]
    fn values() {
        use Value::*;
        let table =
            table(&[("id", DataType::Integer), ("f", DataType::Float), ("s", DataType::String)]);
        let stats = compute(
            &table,
            vec![
                vec![Integer(1), Float(f64::NAN), String("".into())],
                vec![Integer(2), Float(0.0), String("👋👋".into())],
                vec![Integer(3), Float(f64::NAN), String("abc".into())],
                vec![Integer(4), Float(0.0), Null],
                vec![Integer(5), Float(f64::NEG_INFINITY), String("abc".into())],
            ],
        );
        assert_eq!(stats.rows, 5);

        let id = &stats.columns["id"];
        assert_eq!((id.nulls, id.distinct), (0, 5));
        assert_eq!((&id.min, &id.max), (&Some(Integer(1)), &Some(Integer(5))));
        assert_eq!(id.top, Some((Integer(1), 1)));
        assert_eq!(id.avg_length, None);

        // NaN is a single value, sorting last. The tie between 0.0 and NaN
        // goes to the smaller 0.0.
        let f = &stats.columns["f"];
        assert_eq!((f.nulls, f.distinct), (0, 3));
        assert_eq!((&f.min, &f.max), (&Some(Float(f64::NEG_INFINITY)), &Some(Float(f64::NAN))));
        assert_eq!(f.top, Some((Float(0.0), 2)));

        // Lengths are in characters.
        let s = &stats.columns["s"];
        assert_eq!((s.nulls, s.distinct), (1, 3));
        assert_eq!((&s.min, &s.max), (&Some(String("".into())), &Some(String("👋👋".into()))));
        assert_eq!(s.top, Some((String("abc".into()), 2)));
        assert_eq!(s.avg_length, Some(2.0));
    }

    #[test]
    fn display() {
        use Value::*;
        let table = table(&[("id", DataType::Integer), ("s", DataType::String)]);
        let rows = vec![vec![Integer(1), String("ab".into())], vec![Integer(2), Null]];
        let stats = compute(&table, rows);
        assert_eq!(
            stats.to_string(),
            "rows=2 id(nulls=0 distinct=2 min=1 max=2 top=1×1) s(nulls=1 distinct=1 min='ab' max='ab' top='ab'×1 avg_length=2.0)"
        );
    }
}
