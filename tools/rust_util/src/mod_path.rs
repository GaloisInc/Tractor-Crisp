use std::fmt;

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Debug, Hash, Default)]
pub struct ModPath(String);

fn is_well_formed(s: &str) -> bool {
    if s.len() == 0 {
        return true;
    }

    // Note: initial `::` is not allowed.  For a path like `::foo`, the first result from `split`
    // will be the empty string, which will cause this function to return `false`.
    for x in s.split("::") {
        if x.len() == 0 {
            return false;
        }
        if x.contains(':') {
            return false;
        }
    }
    true
}

impl ModPath {
    pub fn new(s: String) -> ModPath {
        assert!(is_well_formed(&s), "string {s:?} is not a well-formed path");
        ModPath(s)
    }

    pub fn from_vec(v: Vec<String>) -> ModPath {
        ModPath::new(v.join("::"))
    }

    pub fn from_iter<'a>(it: impl IntoIterator<Item = &'a str>) -> ModPath {
        let mut mp = ModPath::new(String::new());
        for seg in it {
            mp.push(seg);
        }
        mp
    }

    pub fn push(&mut self, segment: &str) {
        assert!(is_well_formed(segment), "string {segment:?} is not a well-formed path");
        if self.0.len() > 0 {
            self.0.push_str("::");
        }
        self.0.push_str(segment);
    }

    pub fn last(&self) -> Option<&str> {
        let i = self.0.rfind("::")?;
        Some(&self.0[i + 2 ..])
    }

    pub fn pop(&mut self) -> Option<String> {
        let i = self.0.rfind("::")?;
        let seg = self.0[i + 2 ..].to_string();
        self.0.truncate(i);
        Some(seg)
    }

    pub fn iter<'a>(&'a self) -> impl Iterator<Item = &'a str> {
        self.0.split("::")
    }
}

impl fmt::Display for ModPath {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        fmt::Display::fmt(&self.0, f)
    }
}
