-- ==========================================
-- 1. RESET: DROP OLD TABLES
-- ==========================================
DROP TABLE IF EXISTS infusions CASCADE;
DROP TABLE IF EXISTS reservations CASCADE;
DROP TABLE IF EXISTS recipe_items CASCADE;
DROP TABLE IF EXISTS ingredients CASCADE;
DROP TABLE IF EXISTS menu_items CASCADE;

-- ==========================================
-- 2. CREATE TABLES
-- ==========================================
CREATE TABLE reservations (
    id SERIAL PRIMARY KEY,
    reservation_code VARCHAR(8) UNIQUE NOT NULL,
    patron_name VARCHAR(100) NOT NULL,
    patron_email VARCHAR(120) NOT NULL,
    total_investment DOUBLE PRECISION NOT NULL,
    status VARCHAR(50) DEFAULT 'Preparing Order',
    pickup_time VARCHAR(50) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE infusions (
    id SERIAL PRIMARY KEY,
    reservation_id INTEGER NOT NULL,
    foundation VARCHAR(100) NOT NULL,
    sweetener VARCHAR(100) NOT NULL,
    pearls VARCHAR(100) NOT NULL,
    item_total DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    CONSTRAINT fk_reservation FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
);

CREATE TABLE menu_items (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    letter VARCHAR(2) NOT NULL,
    category VARCHAR(50) NOT NULL
);

CREATE TABLE ingredients (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    unit VARCHAR(20) NOT NULL,
    stock DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE recipe_items (
    id SERIAL PRIMARY KEY,
    menu_item_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL,
    quantity_required DOUBLE PRECISION NOT NULL,
    CONSTRAINT fk_menu_item FOREIGN KEY (menu_item_id) REFERENCES menu_items(id) ON DELETE CASCADE,
    CONSTRAINT fk_ingredient FOREIGN KEY (ingredient_id) REFERENCES ingredients(id) ON DELETE CASCADE
);

-- =========================================================
-- 1. PURGE OLD / INVALID MENU ITEMS
-- =========================================================
-- This safely removes old items (like 'Dirty Matcha' or 'Cloud Macchiato') 
-- that are no longer on the physical menu board.
DELETE FROM menu_items 
WHERE name NOT IN (
    'Taro Milktea', 'Okinawa Milktea', 'Wintermelon Milktea', 'Cookies and Cream Milktea', 'Matcha Milktea', 'Dark Belgian Choco', 'Biscoff Milktea',
    'Mocha', 'Caramel Macchiato', 'Iced Americano', 'Cappuccino', 'Coffee Jelly Drink', 'French Vanilla', 'Hazelnut',
    'Ube Milk', 'Mango Milk', 'Strawberry Milk', 'Blueberry Milk',
    'Matcha Latte', 'Matcha Caramel', 'Matcha Strawberry',
    'Lychee Mogu Soda', 'Apple Soda', 'Strawberry Soda', 'Blueberry Soda',
    'Cookies and Cream Frappe', 'Mocha Frappe', 'Coffee Frappe', 'Strawberry Frappe', 'Matcha Frappe', 'Mango Frappe',
    'French Fries (Plain)', 'French Fries (Cheese)', 'French Fries (BBQ)', 'Hash Brown', 'Onion Rings', 'Potato Mojos'
);


-- =========================================================
-- 2. ADD RAW INGREDIENTS (If they don't exist)
-- =========================================================
INSERT INTO ingredients (name, unit, stock) 
VALUES 
    ('Assam Black Tea', 'ml', 10000.0),
    ('Jasmine Green Tea', 'ml', 10000.0),
    ('Fresh Milk', 'ml', 8000.0),
    ('Non-Dairy Creamer', 'grams', 5000.0),
    ('Tapioca Pearls', 'grams', 3000.0),
    ('Brown Sugar Syrup', 'ml', 4000.0),
    ('Wintermelon Syrup', 'ml', 2000.0),
    ('Okinawa Syrup', 'ml', 2000.0),
    ('Cookies & Cream Powder', 'grams', 2000.0),
    ('Matcha Powder', 'grams', 1000.0),
    ('Dark Choco Powder', 'grams', 2000.0),
    ('Taro Paste', 'grams', 1500.0),
    ('Strawberry Syrup', 'ml', 2000.0),
    ('Lychee Syrup', 'ml', 2000.0),
    ('Plastic Cups & Lids', 'pcs', 500.0),
    ('Hash Brown (pcs)', 'pcs', 100.0),
    ('French Fries', 'grams', 5000.0),
    ('Onion Rings', 'grams', 3000.0),
    ('Potato Mojos', 'grams', 3000.0),
    ('Snack Packaging', 'pcs', 500.0),
    ('Cooking Oil', 'ml', 10000.0),
    ('Caramel Syrup', 'ml', 2000.0),
    ('Frappe Base', 'grams', 2000.0),
    ('Nata', 'grams', 3000.0),
    ('Coffee Jelly', 'grams', 3000.0),
    ('Espresso Shot', 'ml', 2000.0),
    ('Biscoff Crumbs', 'grams', 1000.0),
    ('Mocha Syrup', 'ml', 2000.0),
    ('French Vanilla Syrup', 'ml', 2000.0),
    ('Hazelnut Syrup', 'ml', 2000.0),
    ('Ube Syrup', 'ml', 2000.0),
    ('Mango Puree', 'grams', 2000.0),
    ('Blueberry Syrup', 'ml', 2000.0),
    ('Apple Syrup', 'ml', 2000.0),
    ('Soda Water', 'ml', 10000.0)
ON CONFLICT (name) DO NOTHING;


-- =========================================================
-- 3. ADD ALL EXACT MENU ITEMS & SNACKS (Prevents Duplicates)
-- =========================================================
INSERT INTO menu_items (name, price, letter, category)
SELECT * FROM (VALUES 
    -- MILKTEA (Base 49)
    ('Taro Milktea', 49.00, 'T', 'Milktea'),
    ('Okinawa Milktea', 49.00, 'O', 'Milktea'),
    ('Wintermelon Milktea', 49.00, 'W', 'Milktea'),
    ('Cookies and Cream Milktea', 49.00, 'C', 'Milktea'),
    ('Matcha Milktea', 49.00, 'M', 'Milktea'),
    ('Dark Belgian Choco', 49.00, 'D', 'Milktea'),
    ('Biscoff Milktea', 49.00, 'B', 'Milktea'),

    -- COFFEE (Base 49)
    ('Mocha', 49.00, 'M', 'Coffee'),
    ('Caramel Macchiato', 49.00, 'C', 'Coffee'),
    ('Iced Americano', 49.00, 'I', 'Coffee'),
    ('Cappuccino', 49.00, 'C', 'Coffee'),
    ('Coffee Jelly Drink', 49.00, 'C', 'Coffee'),
    ('French Vanilla', 49.00, 'F', 'Coffee'),
    ('Hazelnut', 49.00, 'H', 'Coffee'),

    -- MILK SERIES (Base 59)
    ('Ube Milk', 59.00, 'U', 'Milk Series'),
    ('Mango Milk', 59.00, 'M', 'Milk Series'),
    ('Strawberry Milk', 59.00, 'S', 'Milk Series'),
    ('Blueberry Milk', 59.00, 'B', 'Milk Series'),

    -- MATCHA SERIES (Base 59)
    ('Matcha Latte', 59.00, 'ML', 'Matcha Series'),
    ('Matcha Caramel', 59.00, 'MC', 'Matcha Series'),
    ('Matcha Strawberry', 59.00, 'MS', 'Matcha Series'),

    -- FRUIT SODA (Base 59)
    ('Lychee Mogu Soda', 59.00, 'LM', 'Fruit Soda'),
    ('Apple Soda', 59.00, 'A', 'Fruit Soda'),
    ('Strawberry Soda', 59.00, 'S', 'Fruit Soda'),
    ('Blueberry Soda', 59.00, 'B', 'Fruit Soda'),

    -- FRAPPE (Base 79)
    ('Cookies and Cream Frappe', 79.00, 'CC', 'Frappe'),
    ('Mocha Frappe', 79.00, 'M', 'Frappe'),
    ('Coffee Frappe', 79.00, 'C', 'Frappe'),
    ('Strawberry Frappe', 79.00, 'S', 'Frappe'),
    ('Matcha Frappe', 79.00, 'M', 'Frappe'),
    ('Mango Frappe', 79.00, 'M', 'Frappe'),

    -- SNACKS (Separated Fries Flavors)
    ('French Fries (Plain)', 39.00, 'F', 'Snacks'),
    ('French Fries (Cheese)', 39.00, 'F', 'Snacks'),
    ('French Fries (BBQ)', 39.00, 'F', 'Snacks'),
    ('Hash Brown', 29.00, 'H', 'Snacks'),
    ('Onion Rings', 59.00, 'O', 'Snacks'),
    ('Potato Mojos', 59.00, 'P', 'Snacks')
) AS v(name, price, letter, category)
WHERE NOT EXISTS (
    SELECT 1 FROM menu_items WHERE menu_items.name = v.name
);


-- =========================================================
-- 4. LINK ALL RECIPES (Prevents Duplicates)
-- =========================================================
INSERT INTO recipe_items (menu_item_id, ingredient_id, quantity_required) 
SELECT m.id, i.id, v.qty
FROM (VALUES 
    -- Snacks
    ('Hash Brown', 'Hash Brown (pcs)', 1),
    ('Hash Brown', 'Snack Packaging', 1),
    ('Hash Brown', 'Cooking Oil', 20),
    ('French Fries (Plain)', 'French Fries', 150),
    ('French Fries (Plain)', 'Snack Packaging', 1),
    ('French Fries (Plain)', 'Cooking Oil', 50),
    ('French Fries (Cheese)', 'French Fries', 150),
    ('French Fries (Cheese)', 'Snack Packaging', 1),
    ('French Fries (Cheese)', 'Cooking Oil', 50),
    ('French Fries (BBQ)', 'French Fries', 150),
    ('French Fries (BBQ)', 'Snack Packaging', 1),
    ('French Fries (BBQ)', 'Cooking Oil', 50),
    ('Onion Rings', 'Onion Rings', 150),
    ('Onion Rings', 'Snack Packaging', 1),
    ('Onion Rings', 'Cooking Oil', 50),
    ('Potato Mojos', 'Potato Mojos', 150),
    ('Potato Mojos', 'Snack Packaging', 1),
    ('Potato Mojos', 'Cooking Oil', 50),

    -- Milkteas
    ('Taro Milktea', 'Assam Black Tea', 150),
    ('Okinawa Milktea', 'Assam Black Tea', 150),
    ('Wintermelon Milktea', 'Assam Black Tea', 150),
    ('Matcha Milktea', 'Assam Black Tea', 150),
    ('Cookies and Cream Milktea', 'Assam Black Tea', 150),
    ('Dark Belgian Choco', 'Assam Black Tea', 150),
    ('Biscoff Milktea', 'Assam Black Tea', 150),

    -- Coffee
    ('Mocha', 'Espresso Shot', 30),
    ('Caramel Macchiato', 'Espresso Shot', 30),
    ('Iced Americano', 'Espresso Shot', 30),
    ('Cappuccino', 'Espresso Shot', 30),
    ('Coffee Jelly Drink', 'Espresso Shot', 30),
    ('French Vanilla', 'Espresso Shot', 30),
    ('Hazelnut', 'Espresso Shot', 30),

    -- Milk Series
    ('Ube Milk', 'Fresh Milk', 200),
    ('Mango Milk', 'Fresh Milk', 200),
    ('Strawberry Milk', 'Fresh Milk', 200),
    ('Blueberry Milk', 'Fresh Milk', 200),

    -- Matcha Series
    ('Matcha Latte', 'Matcha Powder', 10),
    ('Matcha Caramel', 'Matcha Powder', 10),
    ('Matcha Strawberry', 'Matcha Powder', 10),

    -- Fruit Soda
    ('Lychee Mogu Soda', 'Soda Water', 200),
    ('Apple Soda', 'Soda Water', 200),
    ('Strawberry Soda', 'Soda Water', 200),
    ('Blueberry Soda', 'Soda Water', 200),

    -- Frappe
    ('Cookies and Cream Frappe', 'Frappe Base', 30),
    ('Mocha Frappe', 'Frappe Base', 30),
    ('Coffee Frappe', 'Frappe Base', 30),
    ('Strawberry Frappe', 'Frappe Base', 30),
    ('Matcha Frappe', 'Frappe Base', 30),
    ('Mango Frappe', 'Frappe Base', 30)

) AS v(item_name, ing_name, qty)
JOIN menu_items m ON m.name = v.item_name
JOIN ingredients i ON i.name = v.ing_name
WHERE NOT EXISTS (
    SELECT 1 FROM recipe_items ri 
    WHERE ri.menu_item_id = m.id AND ri.ingredient_id = i.id
);


-- ============================================================================================= --


-- ==========================================
-- DROP EXISTING TABLES (Optional: Removes old tables to start fresh)
-- ==========================================
DROP TABLE IF EXISTS infusions CASCADE;
DROP TABLE IF EXISTS reservations CASCADE;
DROP TABLE IF EXISTS menu_items CASCADE;
DROP TABLE IF EXISTS ingredients CASCADE;
DROP TABLE IF EXISTS expenses CASCADE;
DROP TABLE IF EXISTS audit_logs CASCADE;
DROP TABLE IF EXISTS system_state CASCADE;

-- ==========================================
-- 1. RESERVATIONS TABLE
-- ==========================================
CREATE TABLE reservations (
    id SERIAL PRIMARY KEY,
    reservation_code VARCHAR(8) UNIQUE NOT NULL,
    patron_name VARCHAR(100) NOT NULL,
    patron_email VARCHAR(120) NOT NULL,
    total_investment NUMERIC(10, 2) NOT NULL,
    status VARCHAR(50) DEFAULT 'Waiting Confirmation',
    pickup_time VARCHAR(50) NOT NULL,
    order_source VARCHAR(30) DEFAULT 'Online',
    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Manila')
);

-- ==========================================
-- 2. INFUSIONS (CART ITEMS) TABLE
-- ==========================================
CREATE TABLE infusions (
    id SERIAL PRIMARY KEY,
    reservation_id INTEGER NOT NULL,
    foundation VARCHAR(100) NOT NULL,
    sweetener VARCHAR(100) NOT NULL DEFAULT '100% Sugar',
    ice_level VARCHAR(50) NOT NULL DEFAULT 'Normal Ice',
    pearls VARCHAR(100) NOT NULL,
    cup_size VARCHAR(20) NOT NULL DEFAULT '16 oz',
    addons VARCHAR(200) NOT NULL DEFAULT '',
    item_total NUMERIC(10, 2) NOT NULL DEFAULT 0.0,
    CONSTRAINT fk_reservation 
        FOREIGN KEY (reservation_id) 
        REFERENCES reservations(id) 
        ON DELETE CASCADE
);

-- ==========================================
-- 3. MENU ITEMS TABLE
-- ==========================================
CREATE TABLE menu_items (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    letter VARCHAR(2) NOT NULL,
    category VARCHAR(50) NOT NULL,
    is_out_of_stock BOOLEAN NOT NULL DEFAULT FALSE
);

-- ==========================================
-- 4. INGREDIENTS TABLE
-- ==========================================
CREATE TABLE ingredients (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    unit VARCHAR(20) NOT NULL,
    stock NUMERIC(10, 2) NOT NULL DEFAULT 0.0
);

-- ==========================================
-- 5. EXPENSES TABLE
-- ==========================================
CREATE TABLE expenses (
    id SERIAL PRIMARY KEY,
    description VARCHAR(200) NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Manila')
);

-- ==========================================
-- 6. AUDIT LOGS TABLE
-- ==========================================
CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    action VARCHAR(100) NOT NULL,
    details VARCHAR(255),
    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Manila')
);

-- ==========================================
-- 7. SYSTEM STATE TABLE
-- ==========================================
CREATE TABLE system_state (
    id SERIAL PRIMARY KEY,
    active_session_id VARCHAR(100),
    last_ping TIMESTAMP
);

-- ==========================================
-- 8. INSERT INITIAL SEED DATA
-- (Matches the Python initialization block)
-- ==========================================

-- Insert Default Ingredients
INSERT INTO ingredients (name, unit, stock) VALUES
('Fresh Milk', 'ml', 8000.0),
('Tapioca Pearls', 'grams', 3000.0)
ON CONFLICT (name) DO NOTHING;

-- Insert Default Menu Items
INSERT INTO menu_items (name, price, letter, category, is_out_of_stock) VALUES
('Dirty Matcha', 49.00, 'D', 'Trending Now', FALSE),
('Biscoff Frappe', 84.00, 'B', 'Trending Now', FALSE),
('Midnight Velvet', 49.00, 'M', 'Signature Series', FALSE),
('Taro Symphony', 49.00, 'T', 'Matcha & Taro', FALSE),
('Strawberry Lychee', 49.00, 'S', 'Fruit Infusions', FALSE),
('French Fries', 39.00, 'F', 'Snacks', FALSE),
('Green Apple Soda', 49.00, 'G', 'Fruit Soda', FALSE),
('Blueberry Soda', 49.00, 'B', 'Fruit Soda', FALSE),
('Lychee Mogu Soda', 49.00, 'L', 'Fruit Soda', FALSE),
('Strawberry Soda', 49.00, 'S', 'Fruit Soda', FALSE),
('Matcha Caramel', 59.00, 'M', 'Matcha & Taro', FALSE),
('Matcha Frappe', 84.00, 'M', 'Matcha & Taro', FALSE),
('Matcha Latte', 59.00, 'M', 'Matcha & Taro', FALSE),
('Blueberry Milk', 59.00, 'B', 'Milk Series', FALSE),
('Hazelnut Milk', 59.00, 'H', 'Milk Series', FALSE),
('Mango Milk', 59.00, 'M', 'Milk Series', FALSE),
('Strawberry Milk', 59.00, 'S', 'Milk Series', FALSE),
('Ube Milk', 59.00, 'U', 'Milk Series', FALSE);

--==========================================--

--production--
postgresql://neondb_owner:npg_kKUrsWNfz93Y@ep-icy-feather-amomr9aq-pooler.c-5.us-east-1.aws.neon.tech/milktea-db?sslmode=require&channel_binding=require


--9599 Tea & Coffee Shop--
postgresql://neondb_owner:npg_kKUrsWNfz93Y@ep-polished-smoke-amuv5fwy-pooler.c-5.us-east-1.aws.neon.tech/milktea-db?sslmode=require&channel_binding=require


--==========================================--