//! CSV lines, used by EXPORT and IMPORT.
//!
//! CSV text is handled one line (record) at a time. Fields are separated by
//! commas. A field is quoted with " if it contains a comma, a quote or a line
//! break (so a line break never separates records), with any " doubled. An
//! unquoted empty field is distinct from a quoted empty field "": the former
//! represents a missing value (SQL NULL), the latter an empty string.

use itertools::Itertools as _;

/// Formats CSV fields as a line. None is written as an unquoted empty field,
/// and a field is quoted if it would otherwise be read back differently.
pub fn format_line<S: AsRef<str>>(fields: impl IntoIterator<Item = Option<S>>) -> String {
    fields.into_iter().map(|field| format_field(field.as_ref().map(AsRef::as_ref))).join(",")
}

/// Formats a single CSV field, quoting it if necessary.
fn format_field(field: Option<&str>) -> String {
    let Some(field) = field else {
        return String::new();
    };
    let quote = field.is_empty() || field.contains([',', '"', '\n', '\r']);
    match quote {
        true => format!("\"{}\"", field.replace('"', "\"\"")),
        false => field.to_string(),
    }
}

/// Parses a CSV line into fields, or returns None if the line is not valid
/// CSV. An unquoted empty field is returned as None. A line always has one
/// more field than it has separating commas, so an empty line is a single
/// None field.
///
/// A quoted field extends to the matching closing quote (where "" stands for
/// one "), which must be followed by a comma or the end of the line. Any other
/// field extends to the next comma and can't contain quotes.
pub fn parse_line(line: &str) -> Option<Vec<Option<String>>> {
    let mut fields = Vec::new();
    let mut chars = line.chars().peekable();
    loop {
        let mut field = String::new();
        if chars.next_if_eq(&'"').is_some() {
            // A quoted field, ending at an unescaped quote.
            loop {
                match chars.next()? {
                    '"' if chars.next_if_eq(&'"').is_some() => field.push('"'),
                    '"' => break,
                    c => field.push(c),
                }
            }
            fields.push(Some(field));
            // The closing quote must be followed by a comma or the end.
            match chars.next() {
                Some(',') => continue,
                Some(_) => return None,
                None => return Some(fields),
            }
        }

        // An unquoted field, ending at a comma or the end of the line.
        loop {
            match chars.next() {
                Some(',') => break,
                Some('"') => return None,
                Some(c) => field.push(c),
                None => {
                    fields.push((!field.is_empty()).then_some(field));
                    return Some(fields);
                }
            }
        }
        fields.push((!field.is_empty()).then_some(field));
    }
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;

    #[test_case(&[] => ""; "no fields")]
    #[test_case(&[None] => ""; "null")]
    #[test_case(&[Some("")] => r#""""#; "empty")]
    #[test_case(&[Some("x y"), None, Some("1.5")] => "x y,,1.5"; "plain")]
    #[test_case(&[Some("a,b"), Some(r#"say "hi""#)] => r#""a,b","say ""hi""""#; "quoted")]
    #[test_case(&[Some("a\nb"), Some("c\rd")] => "\"a\nb\",\"c\rd\""; "line breaks")]
    #[test_case(&[None, None, None] => ",,"; "nulls")]
    fn format(fields: &[Option<&str>]) -> String {
        format_line(fields.iter().copied())
    }

    #[test_case("" => Some(vec![None]); "empty line")]
    #[test_case(r#""""# => Some(vec![Some("".into())]); "empty string")]
    #[test_case("a," => Some(vec![Some("a".into()), None]); "trailing comma")]
    #[test_case(",," => Some(vec![None, None, None]); "nulls")]
    #[test_case(r#"1,"a,b","say ""hi""",x y"# => Some(vec![Some("1".into()), Some("a,b".into()), Some(r#"say "hi""#.into()), Some("x y".into())]); "fields")]
    #[test_case("\"a\nb\"" => Some(vec![Some("a\nb".into())]); "line break")]
    #[test_case(r#""a"# => None; "unterminated")]
    #[test_case(r#"a"b"# => None; "quote in unquoted")]
    #[test_case(r#""a"b"# => None; "character after quote")]
    #[test_case(r#""a" "# => None; "space after quote")]
    fn parse(line: &str) -> Option<Vec<Option<String>>> {
        parse_line(line)
    }

    /// Formatted lines parse back into the same fields.
    #[test]
    fn roundtrip() {
        let fields = vec![None, Some("".into()), Some(",\"\n\r".into()), Some("é".into())];
        assert_eq!(parse_line(&format_line(fields.clone())), Some(fields));
    }
}
