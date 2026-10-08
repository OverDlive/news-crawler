"""Validated Boolean conditions shared by the editor and news collector."""
from __future__ import annotations

import re
import unicodedata


class RuleError(ValueError):
    pass


def quote(term):
    return '"' + term.replace('"', ' ').strip() + '"'


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def term_spans(term, text):
    """Latin abbreviations must not match inside another Latin word."""
    term = normalize(term)
    pattern = re.escape(term)
    if term and term[0].isascii() and term[0].isalnum():
        pattern = r'(?<![a-z0-9_])' + pattern
    if term and term[-1].isascii() and term[-1].isalnum():
        pattern += r'(?![a-z0-9_])'
    return [(match.start(), match.end()) for match in re.finditer(pattern, text)]


def contains_term(term, text):
    return bool(term_spans(term, text))


class Parser:
    def __init__(self, expression):
        if not isinstance(expression, str) or not expression.strip():
            raise RuleError("검색 조건을 입력해 주세요.")
        if len(expression) > 500 or any(ord(c) < 32 for c in expression):
            raise RuleError("검색 조건은 한 줄, 최대 500자까지 입력할 수 있습니다.")
        self.tokens = []
        position = 0
        while position < len(expression):
            if expression[position].isspace():
                position += 1
                continue
            char = expression[position]
            if char == "-":
                self.tokens.append(("NOT", "NOT"))
                position += 1
            elif char in "()":
                self.tokens.append((char, char))
                position += 1
            elif char == '"':
                end = expression.find('"', position + 1)
                if end < 0:
                    raise RuleError('닫는 큰따옴표(")가 없습니다.')
                term = expression[position + 1:end].strip()
                if not term:
                    raise RuleError("빈 문구는 검색할 수 없습니다.")
                self.tokens.append(("term", term))
                position = end + 1
            else:
                match = re.match(r'[^\s()"]+', expression[position:])
                term = match.group()
                position += len(term)
                if term.upper() in ("AND", "OR", "NOT"):
                    self.tokens.append((term.upper(), term.upper()))
                elif term.startswith("-") and len(term) > 1:
                    self.tokens.extend([("NOT", "NOT"), ("term", term[1:])])
                else:
                    if any(c in term for c in ":|&"):
                        raise RuleError("연산자는 AND, OR, NOT을 사용해 주세요. 문구는 큰따옴표로 묶습니다.")
                    self.tokens.append(("term", term))
        if len(self.tokens) > 100:
            raise RuleError("검색 조건이 너무 깁니다. 조건을 나눠 등록해 주세요.")
        self.position = 0
        self.depth = 0

    def kind(self):
        return self.tokens[self.position][0] if self.position < len(self.tokens) else None

    def parse(self):
        node = self.parse_or()
        if self.kind() is not None:
            raise RuleError("괄호 또는 연산자 위치를 확인해 주세요.")
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.kind() == "OR":
            self.position += 1
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_unary()
        while self.kind() == "AND" or self.kind() in ("term", "NOT", "("):
            if self.kind() == "AND":
                self.position += 1
            node = ("and", node, self.parse_unary())
        return node

    def parse_unary(self):
        self.depth += 1
        if self.depth > 12:
            raise RuleError("괄호와 NOT의 중첩은 12단계까지 가능합니다.")
        try:
            kind = self.kind()
            if kind == "NOT":
                self.position += 1
                return ("not", self.parse_unary())
            if kind == "(":
                self.position += 1
                node = self.parse_or()
                if self.kind() != ")":
                    raise RuleError("닫는 괄호가 없습니다.")
                self.position += 1
                return node
            if kind == "term":
                term = self.tokens[self.position][1]
                self.position += 1
                return ("term", term)
            raise RuleError("연산자 앞뒤의 검색어를 확인해 주세요.")
        finally:
            self.depth -= 1


def evaluate(node, text):
    kind = node[0]
    if kind == "term":
        return contains_term(node[1], text)
    if kind == "not":
        return not evaluate(node[1], text)
    if kind == "and":
        return evaluate(node[1], text) and evaluate(node[2], text)
    return evaluate(node[1], text) or evaluate(node[2], text)


def clauses(node, negate=False):
    """Bounded disjunctive normal form; handles grouped NOT with De Morgan."""
    kind = node[0]
    if kind == "term":
        return [[(node[1], negate)]]
    if kind == "not":
        return clauses(node[1], not negate)
    left, right = clauses(node[1], negate), clauses(node[2], negate)
    operation = ("or" if kind == "and" else "and") if negate else kind
    if operation == "or":
        result = left + right
    else:
        if len(left) * len(right) > 32:
            raise RuleError("조건 조합이 32개를 넘습니다. OR 조건을 나눠 등록해 주세요.")
        result = [a + b for a in left for b in right]
    if len(result) > 32:
        raise RuleError("조건 조합이 32개를 넘습니다. OR 조건을 나눠 등록해 주세요.")
    return result


class SearchRule:
    def __init__(self, entry):
        if entry.get("profile") == "company_topic":
            self.init_profile(entry)
            return
        name = entry.get("name", "").strip()
        condition = entry.get("condition", "")
        exclude = entry.get("exclude", "")
        self.scope = entry.get("scope", "title_description")
        if not isinstance(condition, str) or not isinstance(exclude, str):
            raise RuleError("검색 조건과 제외어는 문자열이어야 합니다.")
        if self.scope not in ("title", "title_description"):
            raise RuleError("검색 범위를 확인해 주세요.")
        if len(exclude) > 300 or any(ord(c) < 32 for c in exclude):
            raise RuleError("제외어는 한 줄, 최대 300자까지 입력할 수 있습니다.")
        self.expression = condition.strip() or quote(name)
        self.tree = Parser(self.expression).parse()
        for term in exclude.split(","):
            if term.strip():
                cleaned = term.strip().replace('"', '').strip()
                if not cleaned:
                    raise RuleError("제외어에는 빈 문구를 입력할 수 없습니다.")
                self.tree = ("and", self.tree, ("not", ("term", cleaned)))
        combinations = clauses(self.tree)
        if any(not any(not negative for _, negative in group) for group in combinations):
            raise RuleError("모든 OR 분기에 포함 검색어가 필요합니다. NOT/제외 조건만으로 검색할 수 없습니다.")
        parts = []
        for group in combinations:
            tokens = list(dict.fromkeys(("-" if negative else "") + quote(term) for term, negative in group))
            parts.append(" ".join(tokens))
        self.query = " OR ".join(f"({part})" for part in parts) if len(parts) > 1 else parts[0]
        if len(self.query) > 3500:
            raise RuleError("변환된 검색 조건이 너무 깁니다. 조건을 나눠 등록해 주세요.")
        # Unmodified legacy keywords retain their original search-result behavior.
        self.filter_locally = bool(condition.strip() or exclude.strip() or self.scope == "title")
        self.queries = [self.query]

    def init_profile(self, entry):
        from security_profiles import validate_terms
        aliases = validate_terms(entry.get("aliases"))
        terms = validate_terms(entry.get("terms"))
        category = entry.get("category")
        if (not isinstance(category, str) or not category.strip() or len(category) > 50
                or any(ord(c) < 32 for c in category)
                or not isinstance(entry.get("company"), str) or not entry["company"].strip()):
            raise RuleError("기관와 모니터링 분류를 확인해 주세요.")
        disabled = entry.get("disabled_terms", [])
        if not isinstance(disabled, list) or any(term not in terms for term in disabled):
            raise RuleError("중지한 검색어가 분류 검색어 목록에 없습니다.")
        terms = [term for term in terms if term not in disabled]
        if not terms:
            raise RuleError("검색어를 하나 이상 켜 주세요. 분류 전체는 분류 OFF로 중지할 수 있습니다.")
        # Reuse the ordinary rule's validation for scope and exclusion phrases.
        base = SearchRule({"name": aliases[0], "exclude": entry.get("exclude", ""),
                           "scope": entry.get("scope", "title_description")})
        self.scope = base.scope
        self.company = entry['company']
        self.category = category
        self.aliases = aliases
        self.terms = terms
        def any_of(values):
            node = ("term", values[0])
            for term in values[1:]:
                node = ("or", node, ("term", term))
            return node
        self.tree = ("and", any_of(aliases), any_of(terms))
        exclusions = [x.strip().replace('"', '').strip() for x in entry.get("exclude", "").split(",") if x.strip()]
        self.exclusions = exclusions
        for term in exclusions:
            self.tree = ("and", self.tree, ("not", ("term", term)))
        def group(values):
            return "(" + " OR ".join(quote(x) for x in values) + ")"
        suffix = "".join(" -" + quote(x) for x in exclusions)
        self.expression = group(aliases) + " AND " + group(terms) + suffix
        # Keep requests short and avoid the aliases × topics DNF explosion.
        self.queries = [group(aliases[a:a+4]) + " " + group(terms[t:t+8]) + suffix
                        for a in range(0, len(aliases), 4) for t in range(0, len(terms), 8)]
        self.query = self.expression
        self.filter_locally = True

    def matches(self, title, description=""):
        text = title if self.scope == "title" else title + " " + description
        text = normalize(text)
        if hasattr(self, 'company'):
            matched = bool(self.company_matches(text)) and any(contains_term(term, text) for term in self.terms) and not any(
                contains_term(term, text) for term in self.exclusions)
            if matched:
                from incident_filter import SECURITY_CATEGORIES, incident_relevant
                if self.category in SECURITY_CATEGORIES:
                    return incident_relevant(title, description if self.scope != 'title' else '')
            return matched
        return evaluate(self.tree, text)

    def company_matches(self, text):
        from security_profiles import COMPANIES
        hits = []
        for alias in self.aliases:
            normalized = normalize(alias)
            for start, end in term_spans(alias, text):
                # Korean particles after a company are valid; a prefix inside another word is not.
                if '\uac00' <= normalized[0] <= '\ud7a3' and start and text[start - 1].isalnum():
                    continue
                blocked = False
                for company, others in COMPANIES.items():
                    if company == self.company or any(normalize(company) == normalize(value) for value in self.aliases):
                        continue
                    for other in others:
                        if len(normalize(other)) <= len(normalized) or normalized not in normalize(other):
                            continue
                        if any(a <= start and end <= b for a, b in term_spans(other, text)):
                            blocked = True
                            break
                    if blocked:
                        break
                if not blocked:
                    hits.append(alias)
                    break
        return hits

    def evidence(self, title, description=""):
        if not hasattr(self, 'company'):
            return ''
        text = normalize(title if self.scope == 'title' else title + ' ' + description)
        aliases = self.company_matches(text)
        terms = [term for term in self.terms if contains_term(term, text)]
        return '기관: ' + ', '.join(aliases) + ' / 검색어: ' + ', '.join(terms) if aliases and terms else ''

    def accepts(self, article):
        return not self.filter_locally or self.matches(article.title, article.description)

    @property
    def bing_queries(self):
        """Bing news RSS returns empty feeds for grouped OR expressions.

        Fetch company candidates once per alias, shared across topic rules.
        Ordinary rules use separate positive AND branches. The complete rule,
        including NOT, phrases and scope, is always checked on each candidate.
        """
        if hasattr(self, 'company'):
            return list(dict.fromkeys(quote(alias) for alias in self.aliases))
        return list(dict.fromkeys(' '.join(dict.fromkeys(quote(term) for term, negative in group
                                                        if not negative))
                                  for group in clauses(self.tree)))
