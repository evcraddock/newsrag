# Code Standards

## Formatting

Use Ruff for formatting:

```bash
ruff format .
```

## Linting

Use Ruff for linting:

```bash
ruff check .
```

## Type Hints

Use type hints for all functions:

```python
# ❌ Bad
def create_user(name, age):
    pass

# ✅ Good
def create_user(name: str, age: int) -> User:
    pass
```

### Optional Types

```python
from typing import Optional

def get_user(id: int) -> Optional[User]:
    """Returns None if user not found."""
    pass
```

### Collections

```python
from typing import List, Dict

def get_users() -> List[User]:
    pass

def get_user_map() -> Dict[int, User]:
    pass
```

## Naming

Follow PEP 8:

- `snake_case` for functions, variables, modules
- `PascalCase` for classes
- `SCREAMING_SNAKE_CASE` for constants

```python
MAX_RETRIES = 3

class UserService:
    def create_user(self, name: str) -> User:
        pass
```

## Error Handling

Use specific exceptions:

```python
# ❌ Bad
raise Exception("User not found")

# ✅ Good
class UserNotFoundError(Exception):
    pass

raise UserNotFoundError(f"User {id} not found")
```

Handle exceptions explicitly:

```python
try:
    user = get_user(id)
except UserNotFoundError:
    return None
```

## Functions

- Keep functions focused
- Use docstrings for public functions

```python
def create_user(name: str, email: str) -> User:
    """Create a new user.
    
    Args:
        name: The user's display name.
        email: The user's email address.
        
    Returns:
        The newly created User object.
        
    Raises:
        ValidationError: If email is invalid.
    """
    pass
```

## Testing

Use pytest:

```python
def test_create_user():
    user = create_user("Alice", "alice@example.com")
    
    assert user.name == "Alice"
    assert user.email == "alice@example.com"


def test_create_user_invalid_email():
    with pytest.raises(ValidationError):
        create_user("Alice", "invalid")
```

### Fixtures

```python
@pytest.fixture
def db():
    """Create a test database."""
    db = create_test_db()
    yield db
    db.cleanup()


def test_user_persistence(db):
    user = create_user("Alice", "alice@example.com")
    db.save(user)
    
    loaded = db.get(user.id)
    assert loaded.name == "Alice"
```
