use std::fmt;

/// All errors reported to the user by the CLI.
#[derive(Debug)]
pub enum AppError {
    Driver(typedb_driver::Error),
    Usage(String),
    NotFound(String),
    Csv { file: String, line: usize, message: String },
    /// The database returned data that does not match what the application expects.
    Data(String),
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AppError::Driver(e) => write!(f, "{e}"),
            AppError::Usage(msg) => write!(f, "{msg}"),
            AppError::NotFound(msg) => write!(f, "{msg}"),
            AppError::Csv { file, line, message } => write!(f, "{file}:{line}: {message}"),
            AppError::Data(msg) => write!(f, "{msg}"),
        }
    }
}

impl std::error::Error for AppError {}

impl From<typedb_driver::Error> for AppError {
    fn from(e: typedb_driver::Error) -> Self {
        AppError::Driver(e)
    }
}

pub type Result<T> = std::result::Result<T, AppError>;
